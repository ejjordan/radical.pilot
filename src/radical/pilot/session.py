
__copyright__ = "Copyright 2013-2016, http://radical.rutgers.edu"
__license__   = "MIT"

import os
import copy
import glob
import time

from typing import Optional

import threading as mt

import radical.utils                as ru
import radical.saga                 as rs
import radical.saga.filesystem      as rsfs
import radical.saga.utils.pty_shell as rsup

from .      import constants as rpc
from .      import utils     as rpu
from .proxy import Proxy


# ------------------------------------------------------------------------------
#
class _CloseOptions(ru.TypedDict):
    """Options and validation for Session.close().

    Arguments:
        download (bool, optional): Fetch pilot profiles and database entries.
            (Default False.)
        terminate (bool, optional): Shut down all pilots associated with the
            session. (Default True.)

    """

    _schema = {
        'download' : bool,
        'terminate': bool
    }

    _defaults = {
        'download' : False,
        'terminate': True
    }


    # --------------------------------------------------------------------------
    #
    def __init__(self, from_dict):

        super().__init__(from_dict)
        self._verify()


# ------------------------------------------------------------------------------
#
class Session(rs.Session):
    """Root of RP object hierarchy for an application instance.

    A Session is the root object of all RP objects in an application instance:
    it holds :class:`radical.pilot.PilotManager` and
    :class:`radical.pilot.TaskManager` instances which in turn hold
    :class:`radical.pilot.Pilot` and :class:`radical.pilot.Task`
    instances, and several other components which operate on those stateful
    entities.
    """

    # In that role, the session will create a special pubsub channel `heartbeat`
    # which is used by all components in its hierarchy to exchange heartbeat
    # messages.  Those messages are used to watch component health - if
    # a (parent or child) component fails to send heartbeats for a certain
    # amount of time, it is considered dead and the process tree will terminate.
    # That heartbeat management is implemented in the `ru.Heartbeat` class.
    # Only primary sessions instantiate a heartbeat channel (i.e., only the root
    # sessions of RP client or agent modules), but all components need to call
    # the sessions `heartbeat()` method at regular intervals.

    # the reporter is an application-level singleton
    _reporter = None

    # a session has one of three possible roles:
    #   - primary: the session is the first explicit session instance created in
    #     an RP application.
    #   - agent: the session is the first session instance created in an RP
    #     agent.
    #   - default: any other session instance, for example such as created by
    #     components in the client or agent module.
    _PRIMARY = 'primary'
    _AGENT_0 = 'agent_0'
    _AGENT_N = 'agent_n'
    _DEFAULT = 'default'


    # --------------------------------------------------------------------------
    #
    def __init__(self, proxy_url  : Optional[str ] = None,
                       proxy_host : Optional[str ] = None,
                       uid        : Optional[str ] = None,
                       cfg        : Optional[dict] = None,
                       _role      : Optional[str ] = _PRIMARY,
                       _reg_addr  : Optional[str ] = None,
                       **close_options):
        """Create a new session.

        A new Session instance is created and stored in the database.

        Any RP Session will require an RP Proxy to facilitate communication
        between the client machine (i.e., the host where the application created
        this Session instance) and the target resource (i.e., the host where the
        pilot agent/s is/are running and where the workload is being executed).

        A `proxy_url` can be specified which then must point to an RP Proxy
        Service instance which this session can use to establish a communication
        proxy. Alternatively, a `proxy_host` can be specified - the session will
        then attempt to start a proxy service on that host.  If neither
        `proxy_url` nor `proxy_host` are specified, the session will check for
        the environment variables `RADICAL_PILOT_PROXY_URL` and
        `RADICAL_PILOT_PROXY_HOST` (in that order) and will interpret them as
        above.  If none of these information is available, the session will
        instantiate a proxy service on the local host.  Note that any proxy
        service instantiated by the session itself will be terminated once the
        session instance is closed or goes out of scope and is thus garbage
        collected and as such should not be used by other session instances.

        Note: an RP proxy will have to be accessible by both the client and the
              target hosts to facilitate communication between both parties.
              That implies access to the respective ports.  Proxies started by
              the session itself will use the first port larger than 10.000
              which is found to be free.

        Arguments:

            proxy_url (str, optional): proxy service URL - points to an RP
              proxy service which is used to establish an RP communication proxy
              for this session.

            proxy_host (str, optional): proxy host - alternative to the
              `proxy_url`, the application can specify a host name on which
              a temporary proxy is started by the session.  This default to
              `localhost` (but see remarks above about the interpretation of
              environment variables).

            uid (str, optional): Create a session with this UID.  Session UIDs
                MUST be unique - otherwise they will lead to communication
                conflicts, resulting in undefined behaviours.

            cfg (str | dict, optional): a named or instantiated configuration
                to be used for the session.

            _role (`bool`): only `PRIMARY` sessions created by the original
                application process (via `rp.Session()`), will create proxies
                and Registry Serivices.  `AGENT` sessions will also create
                a Registry but no proxies.  All other `DEFAULT` session
                instances are instantiated internally in processes spawned
                (directly or indirectly) by the initial session, for example in
                some of it's components, or by the RP agent.  Those sessions
                will inherit the original session ID, but will not attempt to
                create a new proxies or registries.

            **close_options (optional): If additional key word arguments are
                provided, they will be used as the default arguments to
                Session.close(). This can be useful when the Session is used as
                a Python context manager, such that close() is called
                automatically at the end of a ``with`` block.

            _reg_addr (str, optional): Non-primary sessions will connect to the
                registry at that endpoint and pull session config and resource
                configurations from there.
        """
        self._t_start = time.time()

        if uid: self._uid = uid
        else  : self._uid = ru.generate_id('rp.session', mode=ru.ID_PRIVATE)

        self._role          = _role
        self._uid           = uid
        self._cfg           = ru.Config(cfg=cfg)
        self._reg_addr      = _reg_addr
        self._proxy_url     = proxy_url
        self._proxy_host    = proxy_host
        self._closed        = False
        self._created       = time.time()
        self._close_options = _CloseOptions(close_options)
        self._close_options.verify()

        self._proxy    = None    # proxy client instance
        self._reg      = None    # registry client instance
        self._pmgrs    = dict()  # map IDs to pmgr instances
        self._tmgrs    = dict()  # map IDs to tmgr instances
        self._cmgr     = None    # only primary sessions have a cmgr


        if self._role == self._PRIMARY:

            # if user did not set a uid, we need to generate a new ID
            if not self._uid:
                self._uid = ru.generate_id('rp.session', mode=ru.ID_PRIVATE)

            self._init_primary()


        elif self._role == self._AGENT_0:

            if self._uid:
                raise ValueError('non-primary sessions need a UID')

            self._init_agent_0()


        elif self._role in [self._AGENT_N, self._DEFAULT]:

            if self._uid:
                raise ValueError('non-primary sessions need a UID')

            self._init_secondary()


        # now we have config and uid - initialize base class (saga session)
        rs.Session.__init__(self, uid=self._uid)

        # start bridges and components
        self._init_components()

        # cache sandboxes etc.
        self._cache_lock = ru.RLock()
        self._cache      = {'endpoint_fs'      : dict(),
                            'resource_sandbox' : dict(),
                            'session_sandbox'  : dict(),
                            'pilot_sandbox'    : dict(),
                            'client_sandbox'   : self._cfg.client_sandbox,
                            'js_shells'        : dict(),
                            'fs_dirs'          : dict()}

        # at this point we have a bridge connection, logger, etc, and are done
        self._prof.prof('session_ok', uid=self._uid)

        if self._role == self._PRIMARY:
            self._rep.ok('>>ok\n')


    # --------------------------------------------------------------------------
    #
    def _init_primary(self):

        # The primary session
        #   - reads session config files
        #   - reads resource config files
        #   - starts the client side registry service
        #   - pushes the configs into that registry
        #   - pushes bridge and component configs into that registry
        #   - starts a ZMQ proxy (or ensures one is up and running)

        # we still call `_init_cfg` to complete missing config settings
        # FIXME: completion only needed by `PRIMARY`
        self._read_cfg()

        # primary sessions create a registry service
        self._start_registry()
        self._init_registry()

        # store the session config in the new registry
        self._reg.put('sid', self._uid)

        # only primary sessions and agent_0 connect to the ZMQ proxy
        self._init_proxy()


    # --------------------------------------------------------------------------
    #
    def _init_agent_0(self):

        # The agent_0 session expects the `cfg` parameter to contain the
        # complete agent config!
        #
        #   - starts the agent side registry service
        #   - separates
        #     - session config (== agent config)
        #     - bridge configs
        #     - component configs
        #     - resource config
        #   - pushes them all into the registry
        #   - connects to the ZMQ proxy for client/agent communication

        self._start_registry()
        self._init_registry()
        self._init_cfg()


    # --------------------------------------------------------------------------
    #
    def _init_secondary(self):

        pass


    # --------------------------------------------------------------------------
    #
    def _start_registry(self):

        # make sure that no other registry is used
        if self._reg_addr:
            raise ValueError('cannot start registry when providing `reg_addr`')

        self._reg_service = ru.zmq.Registry(uid='%s.reg' % self._uid)
        self._reg_service.start()

        self._reg_addr = self._reg_service.addr


    # --------------------------------------------------------------------------
    #
    def _init_registry(self):

        if not self._reg_addr:
            raise ValueError('session needs a registry address')

        # register the session ID as sanity check for non-primary sessions
        self._reg = ru.zmq.RegistryClient(url=self._reg_addr)


    # --------------------------------------------------------------------------
    #
    def _read_cfg(self):

        # NOTE: the `cfg` parameter to the c'tor is overloaded: it can be
        #       a config name (str) or a config dict to be merged into the
        #       default config.
        cfg_name = 'default'
        if isinstance(self._cfg, str):
            cfg_name  = self._cfg
            self._cfg = None

        # load the named config, merge provided config
        self._cfg = ru.Config('radical.pilot.session', name=cfg_name,
                                                       cfg=self._cfg)

        self._rcfgs = ru.Config('radical.pilot.resource', name='*',
                                                          expand=False)

        # expand recfgs for all schema options
        # FIXME: this is ugly
        for site in self._rcfgs:
            for rcfg in self._rcfgs[site].values():
                for schema in rcfg.get('schemas', []):
                    while isinstance(rcfg.get(schema), str):
                        tgt = rcfg[schema]
                        rcfg[schema] = rcfg[tgt]
    # --------------------------------------------------------------------------
    #
    def _init_cfg(self):

        # At this point we have a UID and a valid registry client.  Depending on
        # session role, the session config is initialized in different ways:
        #
        #   - PRIMARY: read from disk
        #   - AGENT  : get cfg dict (agent config staged by client side)
        #   - DEFAULT: fetch from registry
        #
        # The same scheme holds for resource configs.

        # NOTE: `cfg_name` and `cfg` are overloaded, the user cannot point to
        #       a predefined config and amend it at the same time.  This might
        #       be ok for the session, but introduces an API inconsistency.

        # make sure the cfg has the sid set
        self._cfg['sid'] = self._uid

        # we have a minimal config and uid - initialize base class
        rs.Session.__init__(self, uid=self._uid)

        # session path: where to store logfiles etc.
        if self._cfg.path: self._path = self._cfg.path
        else             : self._path = '%s/%s' % (os.getcwd(), self._uid)

        pwd = os.getcwd()

        if not self._cfg.base:
            self._cfg.base = pwd

        if not self._cfg.path:
            self._cfg.path = '%s/%s' % (self._cfg.base, self._cfg.sid)

        if not self._cfg.client_sandbox:
            self._cfg.client_sandbox = pwd


        # change RU defaults to point logfiles etc. to the session sandbox
        def_cfg             = ru.DefaultConfig()
        def_cfg.log_dir     = self._cfg.path
        def_cfg.report_dir  = self._cfg.path
        def_cfg.profile_dir = self._cfg.path

        self._prof = self._get_profiler(name=self._uid)
        self._rep  = self._get_reporter(name=self._uid)
        self._log  = self._get_logger  (name=self._uid,
                                        level=self._cfg.get('debug'))

        from . import version_detail as rp_version_detail
        self._log.info('radical.pilot version: %s', rp_version_detail)
        self._log.info('radical.saga  version: %s', rs.version_detail)
        self._log.info('radical.utils version: %s', ru.version_detail)

        self._log.debug('=== Session(%s, %s)', self._uid, self._role)
        self._log.debug('\n'.join(ru.get_stacktrace()))

        self._prof.prof('session_start', uid=self._uid)

        self._rep.info ('<<new session: ')
        self._rep.plain('[%s]' % self._uid)

        # primary sessions always create a Registry instance
        self._reg_service = ru.zmq.Registry(uid=self._uid + '.reg',
                                            path=self._cfg.path)
        self._reg_service.start()

        self._cfg.reg_addr = self._reg_service.addr

        # always create a registry client
        assert self._cfg.reg_addr
        self._reg = ru.zmq.RegistryClient(url=self._cfg.reg_addr)


        # FIXME MONGODB: to json
        self._metadata = {'radical_stack':
                                     {'rp': rp_version_detail,
                                      'rs': rs.version_detail,
                                      'ru': ru.version_detail}}
                                    # 'py': py_version_detail}}


        # client sandbox: base for relative staging paths
        if self._role == self._PRIMARY:
            if not self._cfg.client_sandbox:
                self._cfg.client_sandbox = os.getcwd()
        else:
            assert self._cfg.client_sandbox


        # cfg setup is complete - push it to the registry.  Make sure the
        # registry state is consistent for this session (== empty)
        if self._reg.get('cfg'):
            raise RuntimeError('primary session: consistency error, cfg exists')

        # The config is inherited by all session components, so update it
        self._cfg.path = self._path  ##
        self._cfg.sid  = self._uid   ##
        self._log.debug('=== 2 %s', self._role)

        self._reg['cfg']   = self._cfg
        self._reg['rcfgs'] = self._rcfgs


    # --------------------------------------------------------------------------
    #
    def _init_proxy(self):

        # need a proxy_url to connect to - get from arg or config (default cfg
        # pulls this from env)
        if not self._proxy_url:
            self._proxy_url = self._cfg.proxy_url

        if not self._proxy_url:

            if self._role in [self._AGENT_0, self._AGENT_N, self._DEFAULT]:
                raise RuntimeError('proxy service URL missing')

            # start a temporary embedded service on the proxy host
            # (defaults to localhost on the default cfg)

            if not self._proxy_host:
                self._proxy_host = self._cfg.proxy_host

            # NOTE: we assume ssh connectivity to the proxy host - but in fact
            #       do allow proxy_host to be a full saga job service URL
            if '://' in self._proxy_host:
                proxy_host_url = ru.Url(self._proxy_host)
            else:
                proxy_host_url = ru.Url()
                proxy_host_url.set_host(self._proxy_host)

            self._proxy_addr   = None
            self._proxy_event  = mt.Event()

            self._proxy_thread = mt.Thread(target=self._run_proxy)
            self._proxy_thread.daemon = True
            self._proxy_thread.start()

            self._proxy_event.wait()
            assert self._proxy_addr

            proxy_url = self._proxy_addr
            os.environ['RADICAL_PILOT_SERVICE_URL'] = proxy_url

        self._log.debug('=== 5 %s', self._role)
        if self._role == self._PRIMARY:
            self._rep.info ('<<bridge     : ')
            self._rep.plain('[%s]' % self._proxy_url)

        self._cfg.proxy_url = self._proxy_url

        # a primary session will create proxy comm channels, an agent session
        # will query the proxy settings from the same service instance.  All
        # other sessions obtain proxy information via the registry
        if self._role == self._PRIMARY:

            self._log.debug('=== 7 %s', self._role)
            # create to session proxy
            try:
                self._proxy = ru.zmq.Client(url=self._cfg.proxy_url)
                response    = self._proxy.request('register', {'sid': self._uid})
                self._reg.put('proxy', response)
                self._log.debug('proxy response: %s', response)
            except:
                self._log.debug('1 ===: %s', '\n'.join(ru.get_stacktrace()))
                self._log.exception('2 === %s', self._role)
                raise


        elif self._role == self._AGENT_0:

            self._log.debug('=== 7 %s', self._role)
            # query the same service to fetch proxy created by primary session
            self._proxy = ru.zmq.Client(url=self._cfg.proxy_url)
            response    = self._proxy.request('lookup', {'sid': self._uid})
            self._reg.put('proxy', response)
            self._log.debug('proxy response: %s', response)

        # all session keep proxy information in the session config
        self._cfg.proxy = self._reg.get('proxy')


    # --------------------------------------------------------------------------
    #
    def _init_components(self):

        if self._role not in [self._PRIMARY, self._AGENT_0, self._AGENT_N]:
            # no components to start
            return


        # primary sessions have a component manager which also manages
        # heartbeat.  'self._cmgr.close()` should be called during termination
        self._cmgr = rpu.ComponentManager(self.uid, self.reg_addr, self._uid)
        self._cmgr.start_bridges(self._cfg.bridges)
        self._cmgr.start_components(self._cfg.components)

        # make sure we send heartbeats to the proxy
        self._run_proxy_hb()

        pwd = self._cfg.path

        # forward any control messages to the proxy
        def fwd_control(topic, msg):
            self._log.debug('=== fwd control %s: %s', topic, msg)
            self._proxy_ctrl_pub.put(rpc.PROXY_CONTROL_PUBSUB, msg)

        self._proxy_ctrl_pub = ru.zmq.Publisher(rpc.PROXY_CONTROL_PUBSUB, path=pwd)
        self._ctrl_sub = ru.zmq.Subscriber(rpc.CONTROL_PUBSUB, path=pwd)
        self._ctrl_sub.subscribe(rpc.CONTROL_PUBSUB, fwd_control)

        # collect any state updates from the proxy
        def fwd_state(topic, msg):
            self._log.debug('=== fwd state   %s: %s', topic, msg)
            self._state_pub.put(topic, msg)

        self._state_pub = ru.zmq.Publisher(rpc.STATE_PUBSUB, path=pwd)
        self._proxy_state_sub = ru.zmq.Subscriber(rpc.PROXY_STATE_PUBSUB, path=pwd)
        self._proxy_state_sub.subscribe(rpc.PROXY_STATE_PUBSUB, fwd_state)


    # --------------------------------------------------------------------------
    # context manager `with` clause
    #
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


    # --------------------------------------------------------------------------
    #
    def close(self, **kwargs):
        """Close the session.

        All subsequent attempts access objects attached to the session will
        result in an error. If cleanup is set to True, the session data is
        removed from the database.

        Arguments:
            terminate (bool, optional): Shut down all pilots associated with the
                session.
            download (bool, optional): Fetch pilot profiles and database
                entries.
        """

        # close only once
        if self._closed:
            return

        if self._role == self._PRIMARY:
            self._rep.info('closing session %s' % self._uid)

        self._log.debug("session %s closing", self._uid)
        self._prof.prof("session_close", uid=self._uid)

        # Merge kwargs with current defaults stored in self._close_options
        self._close_options.update(kwargs)
        self._close_options.verify()

        # to call for `_verify` method and to convert attributes
        # to their types if needed (but None value will stay if it is set)

        options = self._close_options

        for tmgr_uid, tmgr in self._tmgrs.items():
            self._log.debug("session %s closes tmgr   %s", self._uid, tmgr_uid)
            tmgr.close()
            self._log.debug("session %s closed tmgr   %s", self._uid, tmgr_uid)

        for pmgr_uid, pmgr in self._pmgrs.items():
            self._log.debug("session %s closes pmgr   %s", self._uid, pmgr_uid)
            pmgr.close(terminate=options.terminate)
            self._log.debug("session %s closed pmgr   %s", self._uid, pmgr_uid)

        if self._cmgr:
            self._cmgr.close()

        if self._proxy:
            try:
                self._log.debug("session %s closes service", self._uid)
                self._proxy.request('unregister',
                                      {'sid': self._uid})
            except:
                pass

        self._log.debug("session %s closed", self._uid)
        self._prof.prof("session_stop", uid=self._uid)
        self._prof.close()

        self._closed = True

        # after all is said and done, we attempt to download the pilot log- and
        # profiles, if so wanted
        if options.download:

            self._prof.prof("session_fetch_start", uid=self._uid)
            self._log.debug('start download')
            tgt = self._cfg.base
          # # FIXME: MongoDB
          # self.fetch_json    (tgt='%s/%s' % (tgt, self.uid))
          # self.fetch_profiles(tgt=tgt)
          # self.fetch_logfiles(tgt=tgt)

            self._prof.prof("session_fetch_stop", uid=self._uid)

        if self._role == self._PRIMARY:
            self._t_stop = time.time()
            self._rep.info('<<session lifetime: %.1fs'
                          % (self._t_stop - self._t_start))
            self._rep.ok('>>ok\n')

            # dump json
            json = {'session' : self.as_dict(),
                    'pmgr'    : list(),
                    'pilot'   : list(),
                    'tmgr'    : list(),
                    'task'    : list()}

          # json['session']['_id']      = self.uid
            json['session']['type']     = 'session'
            json['session']['uid']      = self.uid
            json['session']['metadata'] = self._metadata

            for fname in glob.glob('%s/pmgr.*.json' % self.path):
                json['pmgr'].append(ru.read_json(fname))

            for fname in glob.glob('%s/pilot.*.json' % self.path):
                json['pilot'].append(ru.read_json(fname))

            for fname in glob.glob('%s/tmgr.*.json' % self.path):
                json['tmgr'].append(ru.read_json(fname))

            for fname in glob.glob('%s/tasks.*.json' % self.path):
                json['task'] += ru.read_json(fname)

            tgt = '%s/%s.json' % (self.path, self.uid)
            ru.write_json(json, tgt)

        if self._closed and self._created:
            self._rep.info('<<session lifetime: %.1fs' %
                           (self._closed - self._created))
        self._rep.ok('>>ok\n')


    # --------------------------------------------------------------------------
    #
    def _run_proxy(self):

        bridge = Proxy()

        try:
            bridge.start()

            self._proxy_addr = bridge.addr
            self._proxy_event.set()

            # run forever until process is interrupted or killed
            while True:
                time.sleep(1)

        finally:
            bridge.stop()
            bridge.wait()


    # --------------------------------------------------------------------------
    #
    def _run_proxy_hb(self):

        self._proxy_heartbeat_thread = mt.Thread(target=self._proxy_hb)
        self._proxy_heartbeat_thread.daemon = True
        self._proxy_heartbeat_thread.start()


    # --------------------------------------------------------------------------
    #
    def _proxy_hb(self):

        while True:

            self._proxy.request('heartbeat', {'sid': self._uid})
            time.sleep(20)


    # --------------------------------------------------------------------------
    #
    def as_dict(self):
        """Returns a Python dictionary representation of the object."""

        object_dict = {
            'uid'      : self._uid,
            'proxy_url': str(self.proxy_url),
            'cfg'      : copy.deepcopy(self._cfg)
        }
        return object_dict


    # --------------------------------------------------------------------------
    #
    @property
    def reg_addr(self):
        return self._cfg.reg_addr


    # --------------------------------------------------------------------------
    #
    @property
    def uid(self):
        return self._uid


    # --------------------------------------------------------------------------
    #
    @property
    def path(self):
        return self._cfg.path


    # --------------------------------------------------------------------------
    #
    @property
    def base(self):
        return self._cfg.base


    # --------------------------------------------------------------------------
    #
    @property
    def proxy_url(self):
        return self._cfg.proxy_url


    # --------------------------------------------------------------------------
    #
    @property
    def cfg(self):
        return self._cfg


    # --------------------------------------------------------------------------
    #
    @property
    def cmgr(self):
        return self._cmgr


    # --------------------------------------------------------------------------
    #
    def _get_logger(self, name, level=None):
        """Get the Logger instance.

        This is a thin wrapper around `ru.Logger()` which makes sure that
        log files end up in a separate directory with the name of `session.uid`.
        """
        return ru.Logger(name=name, ns='radical.pilot', path=self._cfg.path,
                         targets=['.'], level=level)


    # --------------------------------------------------------------------------
    #
    def _get_reporter(self, name):
        """Get the Reporter instance.

        This is a thin wrapper around `ru.Reporter()` which makes sure that
        log files end up in a separate directory with the name of `session.uid`.
        """

        if not self._reporter:
            self._reporter = ru.Reporter(name=name, ns='radical.pilot',
                                         path=self._cfg.path)
        return self._reporter


    # --------------------------------------------------------------------------
    #
    def _get_profiler(self, name):
        """Get the Profiler instance.

        This is a thin wrapper around `ru.Profiler()` which makes sure that
        log files end up in a separate directory with the name of `session.uid`.
        """

        prof = ru.Profiler(name=name, ns='radical.pilot', path=self._cfg.path)

        return prof


    # --------------------------------------------------------------------------
    #
    def inject_metadata(self, metadata):
        """Insert (experiment) metadata into an active session.

        RP stack version info always get added.
        """

        if not isinstance(metadata, dict):
            raise Exception("Session metadata should be a dict!")

        # FIXME MONGODB: to json
      # if self._dbs and self._dbs._c:
      #     self._dbs._c.update({'type'  : 'session',
      #                          "uid"   : self.uid},
      #                         {"$push" : {"metadata": metadata}})


    # --------------------------------------------------------------------------
    #
    def _register_pmgr(self, pmgr):

        self._pmgrs[pmgr.uid] = pmgr


  # # --------------------------------------------------------------------------
  # #
  # def _reconnect_pmgr(self, pmgr):
  #
  #     if not self._dbs.get_pmgrs(pmgr_ids=pmgr.uid):
  #         raise ValueError('could not reconnect to pmgr %s' % pmgr.uid)
  #
  #     self._pmgrs[pmgr.uid] = pmgr
  #
  #
    # --------------------------------------------------------------------------
    #
    def list_pilot_managers(self):
        """Get PilotManager instances.

        Lists the unique identifiers of all :class:`radical.pilot.PilotManager`
        instances associated with this session.

        Returns:
            list[str]: A list of :class:`radical.pilot.PilotManager` uids.

        """

        return list(self._pmgrs.keys())


    # --------------------------------------------------------------------------
    #
    def get_pilot_managers(self, pmgr_uids=None):
        """Get known PilotManager(s).

        Arguments:
            pmgr_uids (str | list[str]): Unique identifier of the PilotManager we want.

        Returns:
            str | list[str]: One or more `radical.pilot.PilotManager` objects.

        """

        return_scalar = False
        if not isinstance(pmgr_uids, list):
            pmgr_uids     = [pmgr_uids]
            return_scalar = True

        if pmgr_uids: pmgrs = [self._pmgrs[uid] for uid in pmgr_uids]
        else        : pmgrs =  list(self._pmgrs.values())

        if return_scalar: return pmgrs[0]
        else            : return pmgrs


    # --------------------------------------------------------------------------
    #
    def _register_tmgr(self, tmgr):

        self._tmgrs[tmgr.uid] = tmgr


  # # --------------------------------------------------------------------------
  # #
  # def _reconnect_tmgr(self, tmgr):
  #
  #     if not self._dbs.get_tmgrs(tmgr_ids=tmgr.uid):
  #         raise ValueError('could not reconnect to tmgr %s' % tmgr.uid)
  #
  #     self._tmgrs[tmgr.uid] = tmgr
  #
  #
    # --------------------------------------------------------------------------
    #
    def list_task_managers(self):
        """Get TaskManager identifiers.

        Lists the unique identifiers of all :class:`radical.pilot.TaskManager`
        instances associated with this session.

        Returns:
            list[str]: A list of :class:`radical.pilot.TaskManager` uids (`list` of `strings`).

        """

        return list(self._tmgrs.keys())


    # --------------------------------------------------------------------------
    #
    def get_task_managers(self, tmgr_uids=None):
        """Get known TaskManager(s).

        Arguments:
            tmgr_uids (str | list[str]): Unique identifier of the TaskManager we want

        Returns:
            radical.pilot.TaskManager | list[radical.pilot.TaskManager]:
                One or more `radical.pilot.TaskManager` objects.

        """

        return_scalar = False
        if not isinstance(tmgr_uids, list):
            tmgr_uids     = [tmgr_uids]
            return_scalar = True

        if tmgr_uids: tmgrs = [self._tmgrs[uid] for uid in tmgr_uids]
        else        : tmgrs =  list(self._tmgrs.values())

        if return_scalar: return tmgrs[0]
        else            : return tmgrs


    # --------------------------------------------------------------------------
    #
    def list_resources(self):
        """Get list of known resource labels.

        Returns a list of known resource labels which can be used in a pilot
        description.
        """

        resources = list()
        for domain in self._rcfgs:
            for host in self._rcfgs[domain]:
                resources.append('%s.%s' % (domain, host))

        return sorted(resources)


    # --------------------------------------------------------------------------
    #
    def get_resource_config(self, resource, schema=None):
        """Returns a dictionary of the requested resource config."""

        domain, host = resource.split('.', 1)
        if domain not in self._rcfgs:
            raise RuntimeError("Resource domain '%s' is unknown." % domain)

        if host not in self._rcfgs[domain]:
            raise RuntimeError("Resource host '%s' unknown." % host)

        resource_cfg = copy.deepcopy(self._rcfgs[domain][host])

        if  not schema:
            if 'schemas' in resource_cfg:
                schema = resource_cfg['schemas'][0]

        if  schema:
            if  schema not in resource_cfg:
                raise RuntimeError("schema %s unknown for resource %s"
                                  % (schema, resource))

            for key in resource_cfg[schema]:
                # merge schema specific resource keys into the
                # resource config
                resource_cfg[key] = resource_cfg[schema][key]

        resource_cfg.label = resource
        return resource_cfg


  # # --------------------------------------------------------------------------
  # #
  # def fetch_json(self, tgt=None):
  #
  #     return rpu.fetch_json(self._uid, tgt=tgt, session=self,
  #                           skip_existing=True)
  #
  #
  # # --------------------------------------------------------------------------
  # #
  # def fetch_profiles(self, tgt=None):
  #
  #     return rpu.fetch_profiles(self._uid, tgt=tgt, session=self,
  #                               skip_existing=True)
  #
  #
  # # --------------------------------------------------------------------------
  # #
  # def fetch_logfiles(self, tgt=None):
  #
  #     return rpu.fetch_logfiles(self._uid, tgt=tgt, session=self,
  #                               skip_existing=True)
  #
  #
    # --------------------------------------------------------------------------
    #
    def _get_client_sandbox(self):
        """Client sandbox path.

        For the session in the client application, this is `os.getcwd()`.  For the
        session in any other component, specifically in pilot components, the
        client sandbox needs to be read from the session config (or pilot
        config).  The latter is not yet implemented, so the pilot can not yet
        interpret client sandboxes.  Since pilot-side staging to and from the
        client sandbox is not yet supported anyway, this seems acceptable
        (FIXME).
        """

        return self._cache['client_sandbox']


    # --------------------------------------------------------------------------
    #
    def _get_resource_sandbox(self, pilot):
        """Global RP sandbox.

        For a given pilot dict, determine the global RP sandbox, based on the
        pilot's 'resource' attribute.
        """

        # FIXME: this should get 'resource, schema=None' as parameters

        resource = pilot['description'].get('resource')
        schema   = pilot['description'].get('access_schema')

        if not resource:
            raise ValueError('Cannot get pilot sandbox w/o resource target')

        # the global sandbox will be the same for all pilots on any resource, so
        # we cache it
        with self._cache_lock:

            if resource not in self._cache['resource_sandbox']:

                # cache miss -- determine sandbox and fill cache
                rcfg   = self.get_resource_config(resource, schema)
                fs_url = rs.Url(rcfg['filesystem_endpoint'])

                # Get the sandbox from either the pilot_desc or resource conf
                sandbox_raw = pilot['description'].get('sandbox')
                if not sandbox_raw:
                    sandbox_raw = rcfg.get('default_remote_workdir', "$PWD")


                # we may need to replace pat elements with data from the pilot
                # description
                if '%' in sandbox_raw:
                    # expand from pilot description
                    expand = dict()
                    for k, v in pilot['description'].items():
                        if v is None:
                            v = ''
                        if k == 'project':
                            if '_' in v and 'ornl' in resource:
                                v = v.split('_')[0]
                            elif '-' in v and 'ncsa' in resource:
                                v = v.split('-')[0]
                        expand['pd.%s' % k] = v
                        if isinstance(v, str):
                            expand['pd.%s' % k.upper()] = v.upper()
                            expand['pd.%s' % k.lower()] = v.lower()
                        else:
                            expand['pd.%s' % k.upper()] = v
                            expand['pd.%s' % k.lower()] = v
                    sandbox_raw = sandbox_raw % expand


                # If the sandbox contains expandables, we need to resolve those
                # remotely.
                #
                # NOTE: this will only work for (gsi)ssh or similar shell
                #       based access mechanisms
                if '$' not in sandbox_raw:
                    # no need to expand further
                    sandbox_base = sandbox_raw

                else:
                    shell = self.get_js_shell(resource, schema)
                    ret, out, _ = shell.run_sync(' echo "WORKDIR: %s"' %
                                                 sandbox_raw)
                    if ret or 'WORKDIR:' not in out:
                        raise RuntimeError("Couldn't get remote workdir.")

                    sandbox_base = out.split(":")[1].strip()
                    self._log.debug("sandbox base %s", sandbox_base)

                # at this point we have determined the remote 'pwd' - the
                # global sandbox is relative to it.
                fs_url.path = "%s/radical.pilot.sandbox" % sandbox_base

                # before returning, keep the URL string in cache
                self._cache['resource_sandbox'][resource] = fs_url

            return self._cache['resource_sandbox'][resource]


    # --------------------------------------------------------------------------
    #
    def get_js_shell(self, resource, schema):

        if resource not in self._cache['js_shells']:
            self._cache['js_shells'][resource] = dict()

        if schema not in self._cache['js_shells'][resource]:

            rcfg   = self.get_resource_config(resource, schema)

            js_url = rcfg['job_manager_endpoint']
            js_url = rcfg.get('job_manager_hop', js_url)
            js_url = rs.Url(js_url)

            elems  = js_url.schema.split('+')

            if   'ssh'    in elems: js_url.schema = 'ssh'
            elif 'gsissh' in elems: js_url.schema = 'gsissh'
            elif 'fork'   in elems: js_url.schema = 'fork'
            elif len(elems) == 1  : js_url.schema = 'fork'
            else: raise Exception("invalid schema: %s" % js_url.schema)

            if js_url.schema == 'fork':
                js_url.host = 'localhost'

            self._log.debug("rsup.PTYShell('%s')", js_url)
            shell = rsup.PTYShell(js_url, self)
            self._cache['js_shells'][resource][schema] = shell

        return self._cache['js_shells'][resource][schema]


    # --------------------------------------------------------------------------
    #
    def get_fs_dir(self, url):

        if url not in self._cache['fs_dirs']:
            self._cache['fs_dirs'][url] = rsfs.Directory(url,
                                               flags=rsfs.CREATE_PARENTS)

        return self._cache['fs_dirs'][url]


    # --------------------------------------------------------------------------
    #
    def _get_session_sandbox(self, pilot):

        # FIXME: this should get 'resource, schema=None' as parameters

        resource = pilot['description'].get('resource')

        if not resource:
            raise ValueError('Cannot get session sandbox w/o resource target')

        with self._cache_lock:

            if resource not in self._cache['session_sandbox']:

                # cache miss
                resource_sandbox      = self._get_resource_sandbox(pilot)
                session_sandbox       = rs.Url(resource_sandbox)
                session_sandbox.path += '/%s' % self.uid

                self._cache['session_sandbox'][resource] = session_sandbox

            return self._cache['session_sandbox'][resource]


    # --------------------------------------------------------------------------
    #
    def _get_pilot_sandbox(self, pilot):

        # FIXME: this should get 'pid, resource, schema=None' as parameters

        pilot_sandbox = pilot.get('pilot_sandbox')
        if str(pilot_sandbox):
            return rs.Url(pilot_sandbox)

        pid = pilot['uid']
        with self._cache_lock:

            if pid not in self._cache['pilot_sandbox']:

                # cache miss
                session_sandbox     = self._get_session_sandbox(pilot)
                pilot_sandbox       = rs.Url(session_sandbox)
                pilot_sandbox.path += '/%s/' % pilot['uid']

                self._cache['pilot_sandbox'][pid] = pilot_sandbox

            return self._cache['pilot_sandbox'][pid]


    # --------------------------------------------------------------------------
    #
    def _get_endpoint_fs(self, pilot):

        # FIXME: this should get 'resource, schema=None' as parameters

        resource = pilot['description'].get('resource')

        if not resource:
            raise ValueError('Cannot get endpoint filesystem w/o resource target')

        with self._cache_lock:

            if resource not in self._cache['endpoint_fs']:

                # cache miss
                resource_sandbox  = self._get_resource_sandbox(pilot)
                endpoint_fs       = rs.Url(resource_sandbox)
                endpoint_fs.path  = ''

                self._cache['endpoint_fs'][resource] = endpoint_fs

            return self._cache['endpoint_fs'][resource]


    # --------------------------------------------------------------------------
    #
    def _get_task_sandbox(self, task, pilot):

        # If a sandbox is specified in the task description, then interpret
        # relative paths as relativet to the pilot sandbox.

        # task sandboxes are cached in the task dict
        task_sandbox = task.get('task_sandbox')
        if task_sandbox:
            return task_sandbox

        # specified in description?
        if not task_sandbox:
            sandbox  = task['description'].get('sandbox')
            if sandbox:
                task_sandbox = ru.Url(self._get_pilot_sandbox(pilot))
                if sandbox[0] == '/':
                    task_sandbox.path = sandbox
                else:
                    task_sandbox.path += '/%s/' % sandbox

        # default
        if not task_sandbox:
            task_sandbox = ru.Url(self._get_pilot_sandbox(pilot))
            task_sandbox.path += "/%s/" % task['uid']

        # cache
        task['task_sandbox'] = str(task_sandbox)

        return task_sandbox


    # --------------------------------------------------------------------------
    #
    def _get_jsurl(self, pilot):
        """Get job service endpoint and hop URL for the pilot's target resource."""

        resrc   = pilot['description']['resource']
        schema  = pilot['description']['access_schema']
        rcfg    = self.get_resource_config(resrc, schema)

        js_url  = rs.Url(rcfg.get('job_manager_endpoint'))
        js_hop  = rs.Url(rcfg.get('job_manager_hop', js_url))

        # make sure the js_hop url points to an interactive access
        # TODO: this is an unreliable heuristics - we should require the js_hop
        #       URL to be specified in the resource configs.
        if   '+gsissh' in js_hop.schema or \
             'gsissh+' in js_hop.schema    : js_hop.schema = 'gsissh'
        elif '+ssh'    in js_hop.schema or \
             'ssh+'    in js_hop.schema    : js_hop.schema = 'ssh'
        else                               : js_hop.schema = 'fork'

        return js_url, js_hop


# ------------------------------------------------------------------------------

