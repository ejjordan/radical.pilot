
__copyright__ = 'Copyright 2014-2016, http://radical.rutgers.edu'
__license__   = 'MIT'


import os
import sys
import copy
import stat
import time
import pprint
import subprocess          as sp
import multiprocessing     as mp

import radical.utils       as ru

from ..   import utils     as rpu
from ..   import states    as rps
from ..   import constants as rpc
from ..db import DBSession

from .    import rm        as rpa_rm
from .    import lm        as rpa_lm


# ------------------------------------------------------------------------------
#
class Agent_0(rpu.Worker):

    '''
    This is the main agent.  It starts sub-agents and watches them.  If any of
    the sub-agents die, it will shut down the other sub-agents and itself.

    This class inherits the rpu.Worker, so that it can use its communication
    bridges and callback mechanisms.  Specifically, it will pull the DB for
    new tasks to be exexuted and forwards them to the agent's component
    network (see `work()`).  It will also watch the DB for any commands to be
    forwarded (pilot termination, task cancelation, etc), and will take care
    of heartbeat messages to be sent to the client module.  To do all this, it
    initializes a DB connection in `initialize()`.
    '''

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session):

        self._cfg     = cfg
        self._pid     = cfg.pid
        self._pmgr    = cfg.pmgr
        self._pwd     = cfg.pilot_sandbox
        self._session = session
        self._log     = session._log

        self._starttime   = time.time()
        self._final_cause = None

        # this is the earliest point to sync bootstrap and agent profiles
        prof = ru.Profiler(ns='radical.pilot', name='agent.0')
        prof.prof('sync_rel', uid=cfg.pid, msg='agent.0')
        prof.prof('hostname', uid=cfg.pid, msg=ru.get_hostname())

        # connect to MongoDB for state push/pull
        self._connect_db()

        # configure RM before component startup, as components need RM
        # information for function (scheduler, executor)
        self._configure_rm()

        # ensure that app communication channels are visible to workload
        self._configure_app_comm()

        # expose heartbeat channel to sub-agents, bridges and components,
        # and start those
        self._cmgr = rpu.ComponentManager(self._cfg)
        self._cfg.heartbeat = self._cmgr.cfg.heartbeat
        self._cmgr.start_bridges()
        self._cmgr.start_components()

        # create the sub-agent configs and start the sub agents
        self._write_sa_configs()
        self._start_sub_agents()   # TODO: move to cmgr?

        # at this point the session is up and connected, and it should have
        # brought up all communication bridges and components.  We are
        # ready to rumble!
        rpu.Worker.__init__(self, self._cfg, session)

        # run our own slow-paced heartbeat monitor to watch pgr heartbeats
        self._hb = ru.Heartbeat(uid=self._pid,
                                timeout=10.0,  # FIXME:  configurable
                                interval=1.0,  # FIXME:  configurable
                                beat_cb=self._hb_check,  # no own heartbeat(pmgr pulls)
                                term_cb=self._hb_term_cb,
                                log=self._log)
        self._hb.start()

        # register pmgr heartbeat
        self._log.info('hb init for %s', self._pmgr)
        self._hb.beat(uid=self._pmgr)


    # --------------------------------------------------------------------------
    #
    def _hb_check(self):

        self._log.debug('hb check')


    # --------------------------------------------------------------------------
    #
    def _hb_term_cb(self):

        self._cmgr.close()
        self._log.warn('hb termination')

        return None


    # --------------------------------------------------------------------------
    #
    def _connect_db(self):

        # TODO: this needs to evaluate the bootstrapper's HOSTPORT
        self._dbs = DBSession(sid=self._cfg.sid, dburl=self._cfg.dburl,
                              cfg=self._cfg, log=self._log)

    # --------------------------------------------------------------------------
    #
    def _configure_rm(self):

        # Create LRMS which will give us the set of agent_nodes to use for
        # sub-agent startup.  Add the remaining LRMS information to the
        # config, for the benefit of the scheduler).
        self._lrms = rpa_rm.RM.create(name=self._cfg.lrms, cfg=self._cfg,
                                      session=self._session)

        # add the resource manager information to our own config
        self._cfg['lrms_info'] = self._lrms.lrms_info


    # --------------------------------------------------------------------------
    #
    def _configure_app_comm(self):

        # if the pilot description contains a request for application comm
        # channels, merge those into the agent config
        #
        # FIXME: this needs to start the app_comm bridges
        app_comm = self._cfg.get('app_comm')
        if app_comm:
            if isinstance(app_comm, list):
                app_comm = {ac: {'bulk_size': 0,
                                 'stall_hwm': 1,
                                 'log_level': 'error'} for ac in app_comm}
            for ac in app_comm:
                if ac in self._cfg['bridges']:
                    raise ValueError('reserved app_comm name %s' % ac)
                self._cfg['bridges'][ac] = app_comm[ac]


        # some of the bridge addresses also need to be exposed to the workload
        if app_comm:
            if 'unit_environment' not in self._cfg:
                self._cfg['unit_environment'] = dict()
            for ac in app_comm:
                if ac not in self._cfg['bridges']:
                    raise RuntimeError('missing app_comm %s' % ac)
                self._cfg['unit_environment']['RP_%s_IN' % ac.upper()] = \
                        self._cfg['bridges'][ac]['addr_in']
                self._cfg['unit_environment']['RP_%s_OUT' % ac.upper()] = \
                        self._cfg['bridges'][ac]['addr_out']


    # --------------------------------------------------------------------------
    #
    def initialize(self):

        # register the command callback which pulls the DB for commands
        self.register_timed_cb(self._agent_command_cb,
                               timer=self._cfg['db_poll_sleeptime'])

        # register idle callback to pull for units
        self.register_timed_cb(self._check_units_cb,
                               timer=self._cfg['db_poll_sleeptime'])

        # registers the staging_input_queue as this is what we want to push
        # units to
        self.register_output(rps.AGENT_STAGING_INPUT_PENDING,
                             rpc.AGENT_STAGING_INPUT_QUEUE)

        # sub-agents are started, components are started, bridges are up: we are
        # ready to roll!  Update pilot state.
        pilot = {'type'             : 'pilot',
                 'uid'              : self._pid,
                 'state'            : rps.PMGR_ACTIVE,
                 'resource_details' : {
                     'lm_info'      : self._lrms.lm_info.get('version_info'),
                     'lm_detail'    : self._lrms.lm_info.get('lm_detail'),
                     'rm_info'      : self._lrms.lrms_info},
                 '$set'             : ['resource_details']}
        self.advance(pilot, publish=True, push=False)


    # --------------------------------------------------------------------------
    #
    def work(self):

        # all work is done in the registered callbacks
        time.sleep(1)


    # --------------------------------------------------------------------------
    #
    def stage_output(self):

        if  os.path.isfile('./staging_output.txt'):

            if not os.path.isfile('./staging_output.tgz'):

                cmd = 'tar zcvf staging_output.tgz $(cat staging_output.txt)'
                out, err, ret = ru.sh_callout(cmd, shell=True)

                if ret:
                    self._log.debug('=== out: %s', out)
                    self._log.debug('=== err: %s', err)
                    self._log.error('=== output tarring failed: %s', cmd)




    # --------------------------------------------------------------------------
    #
    def finalize(self):

        # tar up output staging data
        self._log.debug('=== stage output parent')
        self.stage_output()

        # tear things down in reverse order
        self._hb.stop()
        self._cmgr.close()

        if self._lrms:
            self._lrms.stop()

        if   self._final_cause == 'timeout'  : state = rps.DONE
        elif self._final_cause == 'cancel'   : state = rps.CANCELED
        elif self._final_cause == 'sys.exit' : state = rps.CANCELED
        else                                 : state = rps.FAILED

        # we don't rely on the existence / viability of the update worker at
        # that point.
        self._log.debug('update db state: %s: %s', state, self._final_cause)
        self._update_db(state, self._final_cause)

        # NOTE: we do not push the final pilot state, as that is done by the
        #       bootstrapper *after* this pilot *actually* finished.

        self._log.info('pilot state: %s [%s]', state, self._final_cause)

        out, out, err = None, None, None

        try   : out = open('./agent.0.out', 'r').read(1024)
        except: pass
        try   : err = open('./agent.0.err', 'r').read(1024)
        except: pass
        try   : log = open('./agent.0.log', 'r').read(1024)
        except: pass

        self._dbs._c.update({'type': 'pilot',
                             'uid' : self._pid},
                            {'$set': {'stdout' : rpu.tail(out),
                                      'stderr' : rpu.tail(err),
                                      'logfile': rpu.tail(log)} })


    # --------------------------------------------------------------------------
    #
    def _update_db(self, state, msg=None):

        # NOTE: we do not push the final pilot state, as that is done by the
        #       bootstrapper *after* this poilot *actually* finished.

        self._log.info('pilot state: %s', state)
        self._log.info('rusage: %s', rpu.get_rusage())
        self._log.info(msg)

        if state == rps.FAILED:
            self._log.info(ru.get_trace())

        out = None
        err = None
        log = None

        try   : out = open('./agent_0.out', 'r').read(1024)
        except: pass
        try   : err = open('./agent_0.err', 'r').read(1024)
        except: pass
        try   : log = open('./agent_0.log', 'r').read(1024)
        except: pass

        ret = self._dbs._c.update({'type': 'pilot',
                                   'uid' : self._pid},
                                  {'$set': {'stdout' : rpu.tail(out),
                                            'stderr' : rpu.tail(err),
                                            'logfile': rpu.tail(log)}
                                  })
        self._log.debug('update ret: %s', ret)


    # --------------------------------------------------------------------
    #
    def _write_sa_configs(self):

        # we have all information needed by the subagents -- write the
        # sub-agent config files.

        # write deep-copies of the config for each sub-agent (sans from agent.0)
        for sa in self._cfg.get('agents', {}):

            assert(sa != 'agent.0'), 'expect subagent, not agent.0'

            # use our own config sans agents/components as a basis for
            # the sub-agent config.
            tmp_cfg = copy.deepcopy(self._cfg)
            tmp_cfg['agents']     = dict()
            tmp_cfg['components'] = dict()

            # merge sub_agent layout into the config
            ru.dict_merge(tmp_cfg, self._cfg['agents'][sa], ru.OVERWRITE)

            tmp_cfg['uid']   = sa
            tmp_cfg['aid']   = sa
            tmp_cfg['owner'] = 'agent.0'

            ru.write_json(tmp_cfg, './%s.cfg' % sa)


    # --------------------------------------------------------------------------
    #
    def _start_sub_agents(self):
        '''
        For the list of sub_agents, get a launch command and launch that
        agent instance on the respective node.  We pass it to the seconds
        bootstrap level, there is no need to pass the first one again.
        '''

        # FIXME: reroute to agent daemonizer

        if not self._cfg.get('agents'):
            return

        self._log.debug('start_sub_agents')

        # the configs are written, and the sub-agents can be started.  To know
        # how to do that we create the agent launch method, have it creating
        # the respective command lines per agent instance, and run via
        # popen.
        #
        # actually, we only create the agent_lm once we really need it for
        # non-local sub_agents.
        agent_lm   = None
        for sa in self._cfg['agents']:

            target = self._cfg['agents'][sa]['target']

            if target == 'local':

                # start agent locally
                cmdline = '/bin/sh -l %s/bootstrap_2.sh %s' % (self._pwd, sa)

            elif target == 'node':

                if not agent_lm:
                    agent_lm = rpa_lm.LaunchMethod.create(
                        name    = self._cfg['agent_launch_method'],
                        cfg     = self._cfg,
                        session = self._session)

                node = self._cfg['lrms_info']['agent_nodes'][sa]
                # start agent remotely, use launch method
                # NOTE:  there is some implicit assumption that we can use
                #        the 'agent_node' string as 'agent_string:0' and
                #        obtain a well format slot...
                # FIXME: it is actually tricky to translate the agent_node
                #        into a viable 'slots' structure, as that is
                #        usually done by the schedulers.  So we leave that
                #        out for the moment, which will make this unable to
                #        work with a number of launch methods.  Can the
                #        offset computation be moved to the LRMS?
                bs_name = "%s/bootstrap_2.sh" % (self._pwd)
                ls_name = "%s/%s.sh" % (self._pwd, sa)
                slots = {
                    'cpu_processes'    : 1,
                    'cpu_threads'      : 1,
                    'gpu_processes'    : 0,
                    'gpu_threads'      : 0,
                  # 'nodes'            : [[node[0], node[1], [[0]], []]],
                    'nodes'            : [{'name'    : node[0],
                                           'uid'     : node[1],
                                           'core_map': [[0]],
                                           'gpu_map' : [],
                                           'lfs'     : {'path': '/tmp', 'size': 0}
                                         }],
                    'cores_per_node'   : self._cfg['lrms_info']['cores_per_node'],
                    'gpus_per_node'    : self._cfg['lrms_info']['gpus_per_node'],
                    'lm_info'          : self._cfg['lrms_info']['lm_info'],
                }
                agent_cmd = {
                    'uid'              : sa,
                    'slots'            : slots,
                    'unit_sandbox_path': self._pwd,
                    'description'      : {'cpu_processes'    : 1,
                                          'gpu_process_type' : 'posix',
                                          'gpu_thread_type'  : 'posix',
                                          'executable'       : "/bin/sh",
                                          'mpi'              : False,
                                          'arguments'        : [bs_name, sa],
                                         }
                }
                cmd, hop = agent_lm.construct_command(agent_cmd,
                        launch_script_hop='/usr/bin/env RP_SPAWNER_HOP=TRUE "%s"' % ls_name)

                with open (ls_name, 'w') as ls:
                    # note that 'exec' only makes sense if we don't add any
                    # commands (such as post-processing) after it.
                    ls.write('#!/bin/sh\n\n')
                    for k,v in agent_cmd['description'].get('environment', {}).items():
                        ls.write('export "%s"="%s"\n' % (k, v))
                    ls.write('\n')
                    for pe_cmd in agent_cmd['description'].get('pre_exec', []):
                        ls.write('%s\n' % pe_cmd)
                    ls.write('\n')
                    ls.write('exec %s\n\n' % cmd)
                    st = os.stat(ls_name)
                    os.chmod(ls_name, st.st_mode | stat.S_IEXEC)

                if hop : cmdline = hop
                else   : cmdline = ls_name

            # spawn the sub-agent
            self._log.info ('create sub-agent %s: %s' % (sa, cmdline))

            # ------------------------------------------------------------------
            class _SA(mp.Process):

                def __init__(self, sa, cmd, log):
                    self._sa   = sa
                    self._cmd  = cmd.split()
                    self._log  = log
                    self._proc = None
                    super(_SA, self).__init__(name=sa, log=self._log)
                    self.start()

                    sys.stdout = open('%s.out' % self._ru_name, 'w')
                    sys.stderr = open('%s.err' % self._ru_name, 'w')
                    out = open('%s.out' % self._sa, 'w')
                    err = open('%s.err' % self._sa, 'w')
                    self._proc = sp.Popen(args=self._cmd, stdout=out, stderr=err)

                def run(self):
                    # FIXME: lifetime, use daemon agent launcher
                    while True:
                        time.sleep(0.1)
                        if self._proc.poll() is None:
                            return True   # all is well
                        else:
                            return False  # proc is gone - terminate
            # ------------------------------------------------------------------

            # FIXME: register heartbeats?

        self._log.debug('start_sub_agents done')


    # --------------------------------------------------------------------------
    #
    def _agent_command_cb(self):

        if not self._check_commands(): return False
        if not self._check_state   (): return False

        return True


    # --------------------------------------------------------------------------
    #
    def _check_commands(self):

        # Check if there's a command waiting
        # FIXME: this pull should be done by the update worker, and commands
        #        should then be communicated over the command pubsub
        # FIXME: commands go to pmgr, umgr, session docs
        # FIXME: check if pull/wipe are atomic
        retdoc = self._dbs._c.find_and_modify(
                    query ={'uid'  : self._pid},
                    update={'$set' : {'cmd': []}},  # Wipe content of array
                    fields=['cmd'])

        if not retdoc:
            return True

        for spec in retdoc.get('cmd', []):

            cmd = spec['cmd']
            arg = spec['arg']

            self._log.debug('cmd: %s [%s]', cmd, arg)

            self._prof.prof('cmd', msg="%s : %s" % (cmd, arg), uid=self._pid)

            if cmd == 'heartbeat' and arg['pmgr'] == self._pmgr:
                self._hb.beat(uid=self._pmgr)

            elif cmd == 'cancel_pilot':
                self._log.info('cancel pilot cmd')
                self.publish(rpc.CONTROL_PUBSUB, {'cmd' : 'terminate',
                                                  'arg' : None})
                self._final_cause = 'cancel'
                self.stop()

                return False  # we are done

            elif cmd == 'cancel_units':
                self._log.info('cancel_units cmd')
                self.publish(rpc.CONTROL_PUBSUB, {'cmd' : 'cancel_units',
                                                  'arg' : arg})
            else:
                self._log.warn('could not interpret cmd "%s" - ignore', cmd)

        return True


    # --------------------------------------------------------------------------
    #
    def _check_state(self):

        # Make sure that we haven't exceeded the runtime - otherwise terminate.
        if self._cfg.runtime:
            if time.time() >= self._starttime +  (int(self._cfg.runtime) * 60):
                self._log.info('runtime limit (%ss).', self._cfg.runtime * 60)
                self._final_cause = 'timeout'
                self.stop()
                return False  # we are done

        return True


    # --------------------------------------------------------------------------
    #
    def _check_units_cb(self):

        # Check for compute units waiting for input staging and log pull.
        #
        # FIXME: Unfortunately, 'find_and_modify' is not bulkable, so we have
        #        to use 'find'.  To avoid finding the same units over and over
        #        again, we update the 'control' field *before* running the next
        #        find -- so we do it right here.
        #        This also blocks us from using multiple ingest threads, or from
        #        doing late binding by unit pull :/
        unit_cursor = self._dbs._c.find({'type'    : 'unit',
                                         'pilot'   : self._pid,
                                         'control' : 'agent_pending'})
        if not unit_cursor.count():
            self._log.info('units pulled:    0')
            return True

        # update the units to avoid pulling them again next time.
        unit_list = list(unit_cursor)
        unit_uids = [unit['uid'] for unit in unit_list]

        self._dbs._c.update({'type'  : 'unit',
                             'uid'   : {'$in'     : unit_uids}},
                            {'$set'  : {'control' : 'agent'}},
                            multi=True)

        self._log.info("units pulled: %4d", len(unit_list))
        self._prof.prof('get', msg='bulk: %d' % len(unit_list), uid=self._pid)

        for unit in unit_list:

            # make sure the units obtain env settings (if needed)
            if 'unit_environment' in self._cfg:
                if not unit['description'].get('environment'):
                    unit['description']['environment'] = dict()
                for k,v in self._cfg['unit_environment'].items():
                    unit['description']['environment'][k] = v

            # we need to make sure to have the correct state:
            unit['state'] = rps._unit_state_collapse(unit['states'])
            self._prof.prof('get', uid=unit['uid'])

            # FIXME: raise or fail unit!
            if unit['state'] != rps.AGENT_STAGING_INPUT_PENDING:
                self._log.error('invalid state: %s', (pprint.pformat(unit)))

            unit['control'] = 'agent'

        # now we really own the CUs, and can start working on them (ie. push
        # them into the pipeline).  We don't publish nor profile as advance,
        # since that happened already on the module side when the state was set.
        self.advance(unit_list, publish=False, push=True)

        return True


# ------------------------------------------------------------------------------

