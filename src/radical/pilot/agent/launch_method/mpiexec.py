
__copyright__ = 'Copyright 2016-2023, The RADICAL-Cybertools Team'
__license__   = 'MIT'

from collections import defaultdict

import radical.utils as ru

from .base import LaunchMethod


# ------------------------------------------------------------------------------
#
class MPIExec(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, lm_cfg, rm_info, log, prof):

        self._mpt    : bool  = False
        self._rsh    : bool  = False
        self._ccmrun : str   = ''
        self._dplace : str   = ''
        self._omplace: str   = ''
        self._command: str   = ''

        LaunchMethod.__init__(self, name, lm_cfg, rm_info, log, prof)


    # --------------------------------------------------------------------------
    #
    def _init_from_scratch(self, env, env_sh):

        lm_info = {
            'env'    : env,
            'env_sh' : env_sh,
            'command': ru.which([
                'mpiexec',             # General case
                'mpiexec.mpich',       # Linux, MPICH
                'mpiexec.hydra',       # Linux, MPICH
                'mpiexec.openmpi',     # Linux, MPICH
                'mpiexec-mpich-mp',    # Mac OSX MacPorts
                'mpiexec-openmpi-mp',  # Mac OSX MacPorts
                'mpiexec_mpt',         # Cheyenne (NCAR)
            ]),
            'mpt'    : False,
            'rsh'    : False,
            'ccmrun' : '',
            'dplace' : '',
            'omplace': ''
        }

        if not lm_info['command']:
            raise ValueError('mpiexec not found - cannot start MPI tasks')

        if '_mpt' in self.name.lower():
            lm_info['mpt'] = True

        if '_rsh' in self.name.lower():
            lm_info['rsh'] = True

        # do we need ccmrun or dplace?
        if '_ccmrun' in self.name.lower():
            lm_info['ccmrun'] = ru.which('ccmrun')
            assert lm_info['ccmrun']

        if '_dplace' in self.name.lower():
            lm_info['dplace'] = ru.which('dplace')
            assert lm_info['dplace']

        # cheyenne always needs mpt and omplace
        if 'cheyenne' in ru.get_hostname():
            lm_info['omplace'] = 'omplace'
            lm_info['mpt']     = True

        mpi_version, mpi_flavor = self._get_mpi_info(lm_info['command'])
        lm_info['mpi_version']  = mpi_version
        lm_info['mpi_flavor']   = mpi_flavor

        return lm_info


    # --------------------------------------------------------------------------
    #
    def _init_from_info(self, lm_info):

        self._env         = lm_info['env']
        self._env_sh      = lm_info['env_sh']
        self._command     = lm_info['command']

        assert self._command

        self._mpt         = lm_info['mpt']
        self._rsh         = lm_info['rsh']
        self._dplace      = lm_info['dplace']
        self._ccmrun      = lm_info['ccmrun']

        self._mpi_version = lm_info['mpi_version']
        self._mpi_flavor  = lm_info['mpi_flavor']

        # ensure empty string on unset omplace
        if not lm_info['omplace']:
            self._omplace = ''
        else:
            self._omplace = 'omplace'


    # --------------------------------------------------------------------------
    #
    def finalize(self):

        pass


    # --------------------------------------------------------------------------
    #
    def can_launch(self, task):

        if not task['description']['executable']:
            return False, 'no executable'

        return True, ''


    # --------------------------------------------------------------------------
    #
    def get_launcher_env(self):

        lm_env_cmds = ['. $RP_PILOT_SANDBOX/%s' % self._env_sh]

        # Cheyenne is the only machine that requires mpiexec_mpt.
        # We then have to set MPI_SHEPHERD=true
        if self._mpt:
            lm_env_cmds.append('export MPI_SHEPHERD=true')

        return lm_env_cmds


    # --------------------------------------------------------------------------
    #
    def _get_rank_file(self, slots, uid, sandbox):
        '''
        Rank file:
            rank 0=localhost slots=0,1,2,3
            rank 1=localhost slots=4,5,6,7
        '''
        rf_str = ''
        for rank_id, rank in enumerate(slots['ranks']):
            core_ids = [str(c) for c in rank['core_map'][0]]
            rf_str += 'rank %d=%s slots=%s\n' % \
                      (rank_id, rank['node_name'], ','.join(core_ids))

        rf_name = '%s/%s.rf' % (sandbox, uid)
        with ru.ru_open(rf_name, 'w') as fout:
            fout.write(rf_str)

        return rf_name


    # --------------------------------------------------------------------------
    #
    def _get_host_file(self, slots, uid, sandbox, simple=True):
        '''
        Host file (simple=True):
            localhost
        Host file (simple=False):
            localhost slots=2
        '''
        host_slots = defaultdict(int)
        for rank in slots['ranks']:
            host_slots[rank['node_name']] += len(rank['core_map'])

        if simple:
            hf_str = '%s\n' % '\n'.join(list(host_slots.keys()))
        else:
            hf_str = ''
            for host_name, num_slots in host_slots.items():
                hf_str += '%s slots=%d\n' % (host_name, num_slots)

        hf_name = '%s/%s.hf' % (sandbox, uid)
        with ru.ru_open(hf_name, 'w') as fout:
            fout.write(hf_str)

        return hf_name


    # --------------------------------------------------------------------------
    #
    def get_launch_cmds(self, task, exec_path):

        uid   = task['uid']
        slots = task['slots']
        sbox  = task['task_sandbox_path']

        assert slots.get('ranks'), 'task.slots.ranks not defined'

        cmd_options = '-np %d ' % len(slots['ranks'])

        # check that this implementation allows to use `rankfile` option
        has_rf = bool(ru.sh_callout('%s --help |& grep -- "-rf"' %
                                    self._command, shell=True)[0])
        if has_rf:
            # use rankfile for hosts and cpu-binding
            hosts = set([r['node_name'] for r in slots['ranks']])
            cmd_options += '-H %s '  % ','.join(hosts) + \
                           '-rf %s'  % self._get_rank_file(slots, uid, sbox)
        else:
            # FIXME: add check for PALS implementation
            # use hostfile
            cores_per_rank = len(slots['ranks'][0]['core_map'][0])
            cmd_options += '--hostfile %s ' % \
                           self._get_host_file(slots, uid, sbox) + \
                           '--depth=%d --cpu-bind depth' % cores_per_rank

        if self._omplace:
            cmd_options += ' %s' % self._omplace

        cmd = '%s %s %s' % (self._command, cmd_options, exec_path)
        return cmd.strip()


    # --------------------------------------------------------------------------
    #
    def get_rank_cmd(self):

        # FIXME: we know the MPI flavor, so make this less guesswork

        ret  = 'test -z "$MPI_RANK"  || export RP_RANK=$MPI_RANK\n'
        ret += 'test -z "$PMIX_RANK" || export RP_RANK=$PMIX_RANK\n'
        ret += 'test -z "$PMI_ID"    || export RP_RANK=$PMI_ID\n'
        ret += 'test -z "$PMI_RANK"  || export RP_RANK=$PMI_RANK\n'

        if self._mpt:
            ret += 'test -z "$MPT_MPI_RANK" || export RP_RANK=$MPT_MPI_RANK\n'

        return ret


    # --------------------------------------------------------------------------
    #
    def get_exec(self, task):

        td           = task['description']
        task_exec    = td['executable']
        task_args    = td['arguments']
        task_argstr  = self._create_arg_string(task_args)
        command      = '%s %s' % (task_exec, task_argstr)

        return command.rstrip()


# ------------------------------------------------------------------------------

