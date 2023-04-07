
from typing import Dict, List, Any, Optional

import radical.utils as ru

from .task             import Task
from .task_description import TaskDescription, RAPTOR_WORKER


# ------------------------------------------------------------------------------
#
class Raptor(Task):
    '''
    RAPTOR: 'RAPid Task executOR'

    A `Raptor` can be submitted to a pilot.  It will be associated with
    `RaptorWorker` instances on that pilot and use those workers to rapidly
    execute tasks.  Raptors excel at high throughput execution for large numbers
    of short running tasks.  However, they have limited capabilities with
    respect to managing task data dependencies, multinode tasks, MPI
    executables, and tasks with heterogeneous resource requirements.
    '''

    # --------------------------------------------------------------------------
    #
    def __init__(self, tmgr: object,
                       descr: Dict[str, Any],
                       origin: str) -> None:

        super().__init__(tmgr, descr, origin)


    # --------------------------------------------------------------------------
    #
    def submit_workers(self, descriptions: List[TaskDescription]) -> List[Task]:

        descriptions = ru.as_list(descriptions)

        for td in descriptions:

            raptor_file  = td.get('raptor_file')  or  ''
            raptor_class = td.get('raptor_class') or  'DefaultWorker'

            if not td.get('uid'):
                td.uid = '%s.worker' % self.uid
                # NOTE: ensure uniqueness in tmgr

            if not td.get('executable'):
                td.executable = 'radical-pilot-raptor-worker'

            if not td.get('named_env'):
                td.named_env = 'rp'

            td.environment['PYTHONUNBUFFERED'] = '1'
            td.mode      = RAPTOR_WORKER
            td.uid       = 'raptor_worker.0000'
            td.arguments = [raptor_file, raptor_class, self._uid]

        return self._tmgr.submit_workers(descriptions)


    # --------------------------------------------------------------------------
    #
    def submit_tasks(self, descriptions: List[TaskDescription]) -> List[Task]:

        descriptions = ru.as_list(descriptions)

        for td in descriptions:
            td.raptor_id = self.uid

        return self._tmgr.submit_tasks(descriptions)


    # --------------------------------------------------------------------------
    #
    def rpc(self, rpc: str,
                  args: Optional[Dict[str, Any]] = None
           ) -> Dict[str, Any]:
        '''
        Send a raptor command, wait for the response, and return the result.
        '''

        if not self._pilot:
            raise RuntimeError('not assoigned to a pilot yet, cannot run rpc')

        reply = self._session._dbs.pilot_rpc(self._pilot, self.uid, rpc, args)

        return reply


# ------------------------------------------------------------------------------
#
RaptorMaster = Raptor
'''
The `RaptorMaster` task is an alias for the `Raptor` class and is provided
for naming symmetry with `RaptorWorker`.
'''


# ------------------------------------------------------------------------------
#
class RaptorWorker(Task):
    '''
    '''

    pass


# ------------------------------------------------------------------------------

