#!/usr/bin/env python3

import os
import sys
import time

import radical.utils as ru
import radical.pilot as rp

from radical.pilot import PythonTask


# This script has to run as a task within an pilot allocation, and is
# a demonstration of a task overlay within the RCT framework.
# It will:
#
#   - create a master which bootstraps a specific communication layer
#   - insert n workers into the pilot (again as a task)
#   - perform RPC handshake with those workers
#   - send RPC requests to the workers
#   - terminate the worker
#
# The worker itself is an external program which is not covered in this code.

pytask = PythonTask.pythontask


@pytask
def func_mpi(msg,comm=None,sleep=0):
    # pylint: disable=reimported
    import time
    print('hello %d/%d: %s' % (comm.rank, comm.size, msg))
    time.sleep(sleep)


@pytask
def func_non_mpi(a):
    # pylint: disable=reimported
    import math
    import random
    b = random.random()
    t = math.exp(a * b)
    return t


# ------------------------------------------------------------------------------
#
class MyMaster(rp.raptor.Master):
    '''
    This class provides the communication setup for the task overlay: it will
    set up the request / response communication queues and provide the endpoint
    information to be forwarded to the workers.
    '''

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg):

        self._cnt = 0
        self._submitted = {rp.TASK_EXECUTABLE  : 0,
                           rp.TASK_FUNCTION    : 0,
                           rp.TASK_EVAL        : 0,
                           rp.TASK_EXEC        : 0,
                           rp.TASK_PROC        : 0,
                           rp.TASK_SHELL       : 0}
        self._collected = {rp.TASK_EXECUTABLE  : 0,
                           rp.TASK_FUNCTION    : 0,
                           rp.TASK_EVAL        : 0,
                           rp.TASK_EXEC        : 0,
                           rp.TASK_PROC        : 0,
                           rp.TASK_SHELL       : 0}

        # initialize the task overlay base class.  That base class will ensure
        # proper communication channels to the pilot agent.
        rp.raptor.Master.__init__(self, cfg=cfg)


    # --------------------------------------------------------------------------
    #
    def submit(self):
        pass

    # --------------------------------------------------------------------------
    #
    def request_cb(self, tasks):

        for task in tasks:

            self._log.debug('request_cb %s\n' % (task['uid']))

            mode = task['description']['mode']
            uid  = task['description']['uid']

            self._submitted[mode] += 1
        return tasks


    # --------------------------------------------------------------------------
    #
    def result_cb(self, tasks):

        for task in tasks:

            mode = task['description']['mode']
            self._collected[mode] += 1

            # NOTE: `state` will be `AGENT_EXECUTING`
            self._log.debug('result_cb  %s: %s [%s] [%s]',
                            task['uid'],
                            task['state'],
                            sorted(task['stdout']),
                            task['return_value'])

            print('result_cb %s: %s %s %s' % (task['uid'], task['state'],
                                              task['stdout'],
                                              task['return_value']))


    # --------------------------------------------------------------------------
    #
    def state_cb(self, tasks):

        for task in tasks:
            uid = task['uid']

            if uid.startswith(self._uid + '.task.m.'):
                self._collected[rp.TASK_EXECUTABLE] += 1


# ------------------------------------------------------------------------------
#
if __name__ == '__main__':

    # This master script runs as a task within a pilot allocation.  The purpose
    # of this master is to (a) spawn a set or workers within the same
    # allocation, (b) to distribute work items (`hello` function calls) to those
    # workers, and (c) to collect the responses again.
    cfg_fname    = str(sys.argv[1])
    cfg          = ru.Config(cfg=ru.read_json(cfg_fname))
    cfg.rank     = int(sys.argv[2])

    n_workers  = cfg.n_workers
    nodes_pw   = cfg.nodes_pw
    cpn        = cfg.cpn
    gpn        = cfg.gpn
    descr      = cfg.worker_descr
    pwd        = os.getcwd()

    # one node is used by master.  Alternatively (and probably better), we could
    # reduce one of the worker sizes by one core.  But it somewhat depends on
    # the worker type and application workload to judge if that makes sense, so
    # we leave it for now.

    # create a master class instance - this will establish communication to the
    # pilot agent
    master = MyMaster(cfg)

    # insert `n` worker tasks into the agent.  The agent will schedule (place)
    # those workers and execute them.  Insert one smaller worker (see above)
    # NOTE: this assumes a certain worker size / layout
    print('workers: %d' % n_workers)
    descr['cpu_processes'] = nodes_pw * cpn
    descr['gpu_processes'] = nodes_pw * gpn
  # descr['cpu_processes'] = 28
  # descr['gpu_processes'] = 0
    master.submit_workers(descr=descr, count=n_workers)

    # wait until `m` of those workers are up
    # This is optional, work requests can be submitted before and will wait in
    # a work queue.
  # master.wait(count=nworkers)

    master.start()
    master.submit()
    master.join()
    master.stop()

    # simply terminate
    # FIXME: clean up workers


# ------------------------------------------------------------------------------

