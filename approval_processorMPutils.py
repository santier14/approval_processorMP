description = "utilities for approval_processorMP.py"
author = "Min-A Cho (mina19@umd.edu), Reed Essick (reed.essick@ligo.org)"

#-----------------------------------------------------------------------
# Import packages
#-----------------------------------------------------------------------

from queueItemsAndTasks import * ### DANGEROUS! but should be ok here...
from eventDictClassMethods import *

from ligoMP.lvalert import lvalertMPutils as utils
from ligoMP.lvalert.commands import parseCommand
from ligo.gracedb.rest import GraceDb, HTTPError

import os
import json
import pickle
import urllib
import logging

import ConfigParser

import time
import datetime

import subprocess as sp

import re

import operator

import functools

import random

#-----------------------------------------------------------------------
# Activate a virtualenv in order to be able to use Comet.
#-----------------------------------------------------------------------

VIRTUALENV_ACTIVATOR = "/home/alexander.pace/emfollow_gracedb/cometenv/bin/activate_this.py" ### FIXME: this shouldn't be hard coded like this. 
                                                                                             ### If we need a virtual environment, it should be distributed along with the package.
                                                                                             ### That way, it is straightforward to install and run the code from *any* computer withour modifying the source code
execfile(VIRTUALENV_ACTIVATOR, dict(__file__=VIRTUALENV_ACTIVATOR))

#--------------------
# Definitions of which checks must be satisfied in each state before moving on
#--------------------

# main checks when currentstate of event is new_to_preliminary
new_to_preliminary = [
    'farCheck',
    'labelCheck',
    'injectionCheck'
    ]

# main checks when currentstate of event is preliminary_to_initial
# will add human signoff and advocate checks later in parseAlert after reading config file
preliminary_to_initial = [
    'farCheck',
    'labelCheck',
    'have_lvem_skymapCheck',
    'idq_joint_fapCheck'
    ]

# tasks when currentstate of event is initial_to_update
initial_to_update = [
    'farCheck',
    'labelCheck',
    'have_lvem_skymapCheck'
    ]

#-----------------------------------------------------------------------
# parseAlert
#-----------------------------------------------------------------------
def parseAlert(queue, queueByGraceID, alert, t0, config):
    '''
    the way approval_processorMP digests lvalerts

    --> check if this alert is a command and delegate to parseCommand

    1) instantiates GraceDB client
    2) pulls childConfig settings
    3) makes sure we have the logger
    4) get lvalert specifics
    5) ensure we have the event_dict for the graceid = lvalert['uid']
    6) take proper action depending on the lvalert info coming in and currentstate of the event_dict 
    '''

    #-------------------------------------------------------------------
    # process commands sent via lvalert_commandMP
    #-------------------------------------------------------------------

    if alert['uid'] == 'command': ### this is a command message!
        return parseCommand( queue, queueByGraceID, alert, t0) ### delegate to parseCommand and return

    #-------------------------------------------------------------------
    # extract relevant config parameters and set up necessary data structures
    #-------------------------------------------------------------------

    # instantiate GraceDB client from the childConfig
    client = config.get('general', 'client')
    g = GraceDb(client)

    # get other childConfig settings; save in configdict
    voeventerror_email      = config.get('general', 'voeventerror_email')
    force_all_internal      = config.get('general', 'force_all_internal')
    preliminary_internal    = config.get('general', 'preliminary_internal')
    forgetmenow_timeout     = config.getfloat('general', 'forgetmenow_timeout')
    approval_processorMPfiles = config.get('general', 'approval_processorMPfiles')
    hardware_inj            = config.get('labelCheck', 'hardware_inj')
    default_farthresh       = config.getfloat('farCheck', 'default_farthresh')
    time_duration           = config.getfloat('injectionCheck', 'time_duration')
    humanscimons            = config.get('operator_signoffCheck', 'humanscimons')

    ### extract options about advocates
    advocates      = config.get('advocate_signoffCheck', 'advocates')
    advocate_text  = config.get('advocate_signoffCheck', 'advocate_text')
    advocate_email = config.get('advocate_signoffCheck', 'advocate_email')

    ### extract options for GRB alerts
    em_coinc_text    = config.get('GRB_alerts', 'em_coinc_text')
    grb_online_text  = config.get('GRB_alerts', 'grb_online_text')
    grb_offline_text = config.get('GRB_alerts', 'grb_offline_text')
    grb_email        = config.get('GRB_alerts', 'grb_email')

    ### extract options about idq
    ignore_idq        = config.get('idq_joint_fapCheck', 'ignore_idq')
    default_idqthresh = config.getfloat('idq_joint_fapCheck', 'default_idqthresh')
    idq_pipelines     = config.get('idq_joint_fapCheck', 'idq_pipelines')
    idq_pipelines     = idq_pipelines.replace(' ','')
    idq_pipelines     = idq_pipelines.split(',')

    skymap_ignore_list = config.get('have_lvem_skymapCheck', 'skymap_ignore_list')

    ### set up configdict (passed to local data structure: eventDicts)
    configdict = {
        'force_all_internal'  : force_all_internal,
        'preliminary_internal': preliminary_internal,
        'hardware_inj'        : hardware_inj,
        'default_farthresh'   : default_farthresh,
        'humanscimons'        : humanscimons,
        'advocates'           : advocates,
        'ignore_idq'          : ignore_idq,
        'default_idqthresh'   : default_idqthresh,
        'client'              : client
    }

    # set up logging
    ### FIXME: why not open the logger each time parseAlert is called?
    ###        that would allow you to better control which loggers are necessary and minimize the number of open files.
    ###        it also minimizes the possibility of something accidentally being written to loggers because they were left open.
    ###        what's more, this is a natural place to set up multiple loggers, one for all data and one for data pertaining only to this graceid

    global logger
    if globals().has_key('logger'): # check to see if we have logger
        logger = globals()['logger']
    else: # if not, set one up
        logger = loadLogger(config)
        logger.info('\n{0} ************ approval_processorMP.log RESTARTED ************\n'.format(convertTime()))

    #-------------------------------------------------------------------
    # extract relevant info about this alert
    #-------------------------------------------------------------------

    # get alert specifics and event_dict information
    graceid     = alert['uid']
    alert_type  = alert['alert_type']
    description = alert['description']
    filename    = alert['file']

    #-------------------------------------------------------------------
    # ensure we have an event_dict and ForgetMeNow tracking this graceid
    #-------------------------------------------------------------------

    if alert_type=='new': ### new event -> we must first create event_dict and set up ForgetMeNow queue item for G events

        ### create event_dict
        event_dict = EventDict() # create a new instance of EventDict class which is a blank event_dict
        if re.match('E', graceid): # this is an external GRB trigger
            event_dict.grb_trigger_setup(alert['object'], graceid, g, config, logger) # populate this event_dict with grb trigger info from lvalert
        else:
            event_dict.setup(alert['object'], graceid, configdict, g, config, logger) # populate this event_dict with information from lvalert
        eventDicts[graceid] = event_dict # add the instance to the global eventDicts
        eventDictionaries[graceid] = event_dict.data # add the dictionary to the global eventDictionaries

        ### ForgetMeNow queue item
        item = ForgetMeNow( t0, forgetmenow_timeout, graceid, eventDicts, queue, queueByGraceID, logger)
        queue.insert(item) # add queue item to the overall queue

        ### set up queueByGraceID
        newSortedQueue = utils.SortedQueue() # create sorted queue for event candidate
        newSortedQueue.insert(item) # put ForgetMeNow queue item into the sorted queue
        queueByGraceID[item.graceid] = newSortedQueue # add queue item to the queueByGraceID
        saveEventDicts(approval_processorMPfiles) # trying to see if expirationtime is updated from None

        message = '{0} -- {1} -- Created event dictionary for {1}.'.format(convertTime(), graceid)
        if loggerCheck(event_dict.data, message)==False: ### FIXME? Reed still isn't convinced 'loggerCheck' is a good idea and thinks we should just print everything, always. ### Mina disagrees here; without the loggerCheck there are sometimes the same messages printed ten, twenty times but out of order. very hard to read the logger and understand the event candidate
            logger.info(message)
        else:
            pass

    else: ### not a new alert -> we may already be tracking this graceid

        if eventDicts.has_key(graceid): ### we're already tracking it

            # get event_dict with expirationtime key updated for the rest of parseAlert
            event_dict = eventDicts[graceid]

            # find ForgetMeNow corresponding to this graceid and update expiration time
            for item in queueByGraceID[graceid]:
                if item.name==ForgetMeNow.name: # selects the queue item that is a ForgetMeNow instance
                    item.setExpiration(t0) # updates the expirationtime key
                    queue.resort() ### may be expensive, but is needed to guarantee that queue remains sorted
                    queueByGraceID[graceid].resort()
                    break
            else: ### we couldn't find a ForgetMeNow for this event! Something is wrong!
                os.system('echo \'ForgetMeNow KeyError\' | mail -s \'ForgetMeNow KeyError {0}\' {1}'.format(graceid, advocate_email))       
                raise KeyError('could not find ForgetMeNow for %s'%graceid) ### Reed thinks this is necessary as a safety net. 
                                                                            ### we want the process to terminate if things are not set up correctly to force us to fix it

        else: # event_dict for event candidate does not exist. we need to create it with up-to-date information
            event_dict = EventDict() # create a new instance of the EventDict class which is a blank event_dict
            if re.match('E', graceid):
                event_dict.grb_trigger_setup(g.events(graceid).next(), graceid, g, config, logger)
            else:
                event_dict.setup(g.events(graceid).next(), graceid, configdict, g, config, logger) # fill in event_dict using queried event candidate dictionary
                event_dict.update() # update the event_dict with signoffs and iDQ info
            eventDicts[graceid] = event_dict # add this instance to the global eventDicts
            eventDictionaries[graceid] = event_dict.data # add the dictionary to the global eventDictionaries

            # create ForgetMeNow queue item and add to overall queue and queueByGraceID
            item = ForgetMeNow(t0, forgetmenow_timeout, graceid, eventDicts, queue, queueByGraceID, logger)
            queue.insert(item) # add queue item to the overall queue

            ### set up queueByGraceID
            newSortedQueue = utils.SortedQueue() # create sorted queue for new event candidate
            newSortedQueue.insert(item) # put ForgetMeNow queue item into the sorted queue
            queueByGraceID[item.graceid] = newSortedQueue # add queue item to the queueByGraceID

            message = '{0} -- {1} -- Created event dictionary for {1}.'.format(convertTime(), graceid)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
            else:
                pass

    #--------------------
    # ignore alerts that are not relevant, like simulation or MDC events
    #--------------------

    # if the graceid starts with 'M' for MDCs or 'S' for Simulation, ignore
    if re.match('M', graceid) or re.match('S', graceid): ### FIXME: we want to make this a config-file option!
        message = '{0} -- {1} -- Mock data challenge or simulation. Ignoring.'.format(convertTime(), graceid)
        if loggerCheck(event_dict.data, message)==False:
            logger.info(message)
        else:
            pass
        saveEventDicts(approval_processorMPfiles)
        return 0

    #--------------------
    # take care of external GRB triggers
    #--------------------
    if re.match('E', graceid):
        # if it's not a log message updating us about possible coincidence with gravitational-wave triggers OR labels we are not interested
        if alert_type=='label':
            record_label(event_dict.data, description)
        if alert_type=='update':
            if 'comment' in alert['object'].keys():
                comment = alert['object']['comment']
                if re.match('coinc', comment): # XXX: find out from Alex what the comments will look like
                    issuer = alert['object']['issuer']
                    record_coinc_info(event_dict.data, comment, issuer, logger)
                    # XXX populate the correct json textfields depending on the issuer OR what the comment looks like
            else:
                pass
        return 0

    #--------------------
    # Appending which checks must be satisfied in preliminary_to_initial state before moving on
    #--------------------

    if humanscimons=='yes':
        preliminary_to_initial.append('operator_signoffCheck')
    if advocates=='yes':
        preliminary_to_initial.append('advocate_signoffCheck')

    #--------------------
    # update information based on the alert_type
    # includes extracting information from the alert
    # may also include generating VOEvents and issuing them
    #--------------------

    # actions for each alert_type
    currentstate = event_dict.data['currentstate'] ### actions depend on the current state
       
    ### NOTE: we handle alert_type=="new" above as well and this conditional is slightly redundant...
    if alert_type=='new':

        #----------------
        ### pass event through PipelineThrottle
        #----------------

        ### check if a PipelineThrottle exists for this node
        group    = event_dict.data['group']
        pipeline = event_dict.data['pipeline']
        search   = event_dict.data['search']
        key = generate_ThrottleKey(group, pipeline, search=search)
        if queueByGraceID.has_key(key): ### a throttle already exists
            if len(queueByGraceID[key]) > 1:
                raise ValueError('too many QueueItems in SortedQueue for pipelineThrottle key=%s'%key)
            item = queueByGraceID[key][0] ### we expect there to be only one item in this SortedQueue

        else: ### we need to make a throttle!
            # pull PipelineThrottle parameters from the config
            if config.has_section(key):
                throttleWin          = config.getfloat(key, 'throttleWin')
                targetRate           = config.getfloat(key, 'targetRate')
                requireManualReset   = config.get(key, 'requireManualReset')
                conf                 = config.getfloat(key, 'conf')

            else:
                throttleWin          = config.getfloat('default_PipelineThrottle', 'throttleWin')
                targetRate           = config.getfloat('default_PipelineThrottle', 'targetRate')
                requireManualReset   = config.get('default_PipelineThrottle', 'requireManualReset')
                conf                 = config.getfloat('default_PipelineThrottle', 'conf')
            item = PipelineThrottle(t0, throttleWin, targetRate, group, pipeline, search=search, requireManualReset=False, conf=0.9, graceDB_url=client)

            queue.insert( item ) ### add to overall queue

            newSortedQueue = utils.SortedQueue() # create sorted queue for event candidate
            newSortedQueue.insert(item) # put ForgetMeNow queue item into the sorted queue
            queueByGraceID[item.graceid] = newSortedQueue # add queue item to the queueByGraceID

        item.addEvent( graceid, t0 ) ### add new event to throttle
                                       ### this takes care of labeling in gracedb as necessary

        if item.isThrottled(): 
            ### send some warning message?
            return 0 ### we're done here because we're ignoring this event -> exit from parseAlert

        #----------------
        ### pass data to Grouper
        #----------------
#        raise Warning("Grouper is not implemented yet! we're currently using a temporate groupTag and prototype code")

        '''
        need to extract groupTag from group_pipeline[_search] mapping. 
            These associations should be specified in the config file, so we'll have to specify this somehow.
            probably just a "Grouper" section, with (option = value) pairs that look like (groupTag = nodeA nodeB nodeC ...)
        '''
        groupTag = 'TEMPORARY'

        ### check to see if Grouper exists for this groupTag
        if queueByGraceID.has_key(groupTag): ### at least one Grouper already exists

            ### determine if any of the existing Groupers are still accepting new triggers
            for item in queueByGraceID[groupTag]:
                if item.isOpen():
                    break ### this Grouper is still open, so we'll just use it
            else: ### no Groupers are open, so we need to create one
                item = Grouper(t0, grouperWin, groupTag, eventDicts, graceDB_url=client) ### create the actual QueueItem

                queue.insert( item ) ### insert it in the overall queue

                newSortedQueue = utils.SortedQueue() ### set up the SortedQueue for queueByGraceID
                newSortedQueue.insert(item)
                queueByGraceID[groupTag] = newSortedQueue  

        else: ### we need to make a Grouper
            grouperWin = config.getfloat('grouper', 'grouperWin')
            item = Grouper(t0, grouperWin, groupTag, eventDicts, graceDB_url=client) ### create the actual QueueItem

            queue.insert( item ) ### insert it in the overall queue

            newSortedQueue = utils.SortedQueue() ### set up the SortedQueue for queueByGraceID
            newSortedQueue.insert(item)
            queueByGraceID[groupTag] = newSortedQueue

        item.addEvent( graceid ) ### add this graceid to the item

        return 0 ### we're done here. When Grouper makes a decision, we'll tick through the rest of the processes with a "selected" label

    elif alert_type=='label':
        record_label(event_dict.data, description)

        if description=='PE_READY': ### PE_READY label was just applied. We may need to send an update alert

            message = '{0} -- {1} -- Sending update VOEvent.'.format(convertTime(), graceid)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
                process_alert(event_dict.data, 'update', g, config, logger)

            else:
                pass

            message = '{0} -- {1} -- State: {2} --> complete.'.format(convertTime(), graceid, currentstate)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
                event_dict.data['currentstate'] = 'complete'

            else:
                pass

        elif description=='EM_READY': ### EM_READY label was just applied. We may need to send an initial alert
            message = '{0} -- {1} -- Sending initial VOEvent.'.format(convertTime(), graceid)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
                process_alert(event_dict.data, 'initial', g, config, logger)

            else:
                pass

            message = '{0} -- {1} -- State: {2} --> initial_to_update.'.format(convertTime(), graceid, currentstate)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
                event_dict.data['currentstate'] = 'initial_to_update'

            else:
                pass

        elif description=="EM_Throttled": ### the event is throttled and we need to turn off all processing for it

            event_dict.data['currentstate'] = 'throttled' ### update current state
            
            ### check if we need to send retractions
            voevents = event_dict.data['voevents']
            if len(voevents) > 0:
                if 'retraction' not in sorted(voevents)[-1]:
                    # there are existing VOEvents we've sent, but no retraction alert
                    process_alert(event_dict.data, 'retraction', g, config, logger)

            ### update ForgetMeNow expiration to handle all the clean-up?
            ### we probably do NOT want to change the clean-up schedule because we'll still likely receive a lot of alerts about this guy
            ### therefore, we just retain the local data and ignore him, rather than erasing the local data and having to query to reconstruct it repeatedly as new alerts come in
#            for item in queueByGraceID[graceid]: ### update expiration of the ForgetMeNow so it is immediately processed next.
#                if item.name == ForgetMeNow.name:
#                    time.setExpiration(-np.infty )
#                                                                ### FIXME: this can break the order in SortedQueue's. We need to pop and reinsert or call a manual resort
#                    queue.resort() ### may be expensive but is needed to guarantee that queue remains sorted
#                    queueByGraceID[graceid].resort()
#                    break
#            else:
#                raise ValueError('could not find ForgetMeNow QueueItem for graceid=%s'%graceid)

        elif description=="EM_Selected": ### this event was selected by a Grouper 
            raise NotImplementedError('write logic to handle \"Selected\" labels')

        elif description=="EM_Superseded": ### this event was superceded by another event within Grouper
            raise NotImplementedError('write logic to handle \"Superseded" labels')

        elif (checkLabels(description.split(), config) > 0): ### some other label was applied. We may need to issue a retraction notice.
            event_dict.data['currentstate'] = 'rejected'

            ### check to see if we need to send a retraction
            voevents = event_dict.data['voevents']
            if len(voevents) > 0:
                if 'retraction' not in sorted(voevents[-1]):
                    # there are existing VOEvents we've sent, but no retraction alert
                    process_alert(event_dict.data, 'retraction', g, config, logger)

        saveEventDicts(approval_processorMPfiles) ### save the updated eventDict to disk
        return 0

    ### FIXME: Reed left off commenting here...









    elif alert_type=='update':
        # first the case that we have a new lvem skymap
        if (filename.endswith('.fits.gz') or filename.endswith('.fits')):
            if 'lvem' in alert['object']['tag_names']: # we only care about skymaps tagged lvem for sharing with MOU partners
                submitter = alert['object']['issuer']['display_name'] # in the past, we used to care who submitted skymaps; keeping this functionality just in case
                record_skymap(event_dict.data, filename, submitter, logger)
            else:
                pass
        # interested in iDQ information
        else:
            if 'comment' in alert['object'].keys():
                comment = alert['object']['comment']
                if re.match('minimum glitch-FAP', comment): # looking to see if it's iDQ glitch-FAP information
                    record_idqvalues(event_dict.data, comment, logger)
                elif re.match('resent VOEvent', comment): # looking to see if another running instance of approval_processorMP sent a VOEvent
                    response = re.findall(r'resent VOEvent (.*) in (.*)', comment) # extracting which VOEvent was re-sent
                    event_dict.data[response[0][1]].append(response[0][0])
                    saveEventDicts(approval_processorMPfiles)
                else:
                    pass

    elif alert_type=='signoff':
        signoff_object = alert['object']
        record_signoff(event_dict.data, signoff_object)

    #---------------------------------------------
    # run checks specific to currentstate of the event candidate
    #---------------------------------------------

    passedcheckcount = 0

    if currentstate=='new_to_preliminary':
        for Check in new_to_preliminary:
            eval('event_dict.{0}()'.format(Check))
            checkresult = event_dict.data[Check + 'result']
            if checkresult==None:
                pass
            elif checkresult==False:
                # because in 'new_to_preliminary' state, no need to apply DQV label
                message = '{0} -- {1} -- Failed {2} in currentstate: {3}.'.format(convertTime(), graceid, Check, currentstate)
                if loggerCheck(event_dict.data, message)==False:
                    logger.info(message)
                else:
                    pass
                message = '{0} -- {1} -- State: {2} --> rejected.'.format(convertTime(), graceid, currentstate)
                if loggerCheck(event_dict.data, message)==False:
                    logger.info(message)
                    event_dict.data['currentstate'] = 'rejected'
                else:
                    pass
                saveEventDicts(approval_processorMPfiles)
                return 0
            elif checkresult==True:
                passedcheckcount += 1
        if passedcheckcount==len(new_to_preliminary):
            message = '{0} -- {1} -- Passed all {2} checks.'.format(convertTime(), graceid, currentstate)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
            else:
                pass
            message = '{0} -- {1} -- Sending preliminary VOEvent.'.format(convertTime(), graceid)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
                process_alert(event_dict.data, 'preliminary', g, config, logger)
            else:
                pass
            message = '{0} -- {1} -- State: {2} --> preliminary_to_initial.'.format(convertTime(), graceid, currentstate)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
                event_dict.data['currentstate'] = 'preliminary_to_initial'
            else:
                pass
            # notify the operators
            instruments = event_dict.data['instruments']
            for instrument in instruments:
                message = '{0} -- {1} -- Labeling {2}OPS.'.format(convertTime(), graceid, instrument)
                if loggerCheck(event_dict.data, message)==False:
                    logger.info(message)
                    g.writeLabel(graceid, '{0}OPS'.format(instrument))
                else:
                    pass
            # notify the advocates
            message = '{0} -- {1} -- Labeling ADVREQ.'.format(convertTime(), graceid, instrument)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
                g.writeLabel(graceid, 'ADVREQ')
                os.system('echo \'{0}\' | mail -s \'{1} passed criteria for follow-up\' {2}'.format(advocate_text, graceid, advocate_email))
                # expose event to LV-EM
                url_perm_base = g.service_url + urllib.quote('events/{0}/perms/gw-astronomy:LV-EM:Observers/'.format(graceid))
                for perm in ['view', 'change']:
                    url = url_perm_base + perm
                    #g.put(url)
            else:
                pass
        saveEventDicts(approval_processorMPfiles)
        return 0

    elif currentstate=='preliminary_to_initial':
        for Check in preliminary_to_initial:
            eval('event_dict.{0}()'.format(Check))
            checkresult = event_dict.data[Check + 'result']
            if checkresult==None:
                pass
            elif checkresult==False:
               # need to set DQV label
                message = '{0} -- {1} -- Failed {2} in currentstate: {3}.'.format(convertTime(), graceid, Check, currentstate)
                if loggerCheck(event_dict.data, message)==False:
                    logger.info(message)
                else:
                    pass
                message = '{0} -- {1} -- State: {2} --> rejected.'.format(convertTime(), graceid, currentstate)
                if loggerCheck(event_dict.data, message)==False:
                    logger.info(message)
                    event_dict.data['currentstate'] = 'rejected'
                else:
                    pass
                message = '{0} -- {1} -- Labeling DQV.'.format(convertTime(), graceid)
                if loggerCheck(event_dict.data, message)==False:
                    logger.info(message)
                    g.writeLabel(graceid, 'DQV')
                else:
                    pass
                saveEventDicts(approval_processorMPfiles)
                return 0
            elif checkresult==True:
                passedcheckcount += 1
                if Check=='have_lvem_skymapCheck': # we want to send skymaps out as quickly as possible, even if humans have not vetted the event
                    process_alert(event_dict.data, 'preliminary', g, config, logger) # if it turns out we've sent this alert with this skymap before, the process_alert function will just not send this repeat
        if passedcheckcount==len(preliminary_to_initial):
            message = '{0} -- {1} -- Passed all {2} checks.'.format(convertTime(), graceid, currentstate)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
            else:
                pass
            message = '{0} -- {1} -- Labeling EM_READY.'.format(convertTime(), graceid)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
                g.writeLabel(graceid, 'EM_READY')
            else:
                pass
        saveEventDicts(approval_processorMPfiles)
        return 0

    elif currentstate=='initial_to_update':
        for Check in initial_to_update:
            eval('event_dict.{0}()'.format(Check))
            checkresult = event_dict.data[Check + 'result']
            if checkresult==None:
                pass
            elif checkresult==False:
               # need to set DQV label
                message = '{0} -- {1} -- Failed {2} in currentstate: {3}.'.format(convertTime(), graceid, Check, currentstate)
                if loggerCheck(event_dict.data, message)==False:
                    logger.info(message)
                else:
                    pass
                message = '{0} -- {1} -- State: {2} --> rejected.'.format(convertTime(), graceid, currentstate)
                if loggerCheck(event_dict.data, message)==False:
                    logger.info(message)
                    event_dict.data['currentstate'] = 'rejected'
                else:
                    pass
                message = '{0} -- {1} -- Labeling DQV.'.format(convertTime(), graceid)
                if loggerCheck(event_dict.data, message)==False:
                    logger.info(message)
                    g.writeLabel(graceid, 'DQV')
                else:
                    pass
                saveEventDicts(approval_processorMPfiles)
                return 0
            elif checkresult==True:
                passedcheckcount += 1
        if passedcheckcount==len(initial_to_update):
            message = '{0} -- {1} -- Passed all {2} checks.'.format(convertTime(), graceid, currentstate)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
            else:
                pass
            message = '{0} -- {1} -- Labeling PE_READY.'.format(convertTime(), graceid)
            if loggerCheck(event_dict.data, message)==False:
                logger.info(message)
                g.writeLabel(graceid, 'PE_READY')
            else:
                pass
        saveEventDicts(approval_processorMPfiles)
        return 0
    
    else:
        saveEventDicts(approval_processorMPfiles)
        return 0
