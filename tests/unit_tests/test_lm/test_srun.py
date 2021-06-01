# pylint: disable=protected-access, unused-argument, no-value-for-parameter

from unittest import mock, TestCase

from .test_common import setUp
from radical.pilot.agent.launch_method.srun import Srun


class TestSrun(TestCase):

    # --------------------------------------------------------------------------
    #
    @mock.patch.object(Srun, '__init__', return_value=None)
    @mock.patch('radical.utils.which', return_value='/bin/srun')
    @mock.patch('radical.utils.sh_callout', return_value=['19.05.2', '', 0])
    @mock.patch('radical.utils.Logger')
    def test_init_from_scratch(self, mocked_logger, mocked_sh_callout,
                               mocked_which, mocked_init):

        lm_srun = Srun(name=None, lm_cfg={}, cfg={}, log=None, prof=None)
        lm_srun.name = 'SRUN'
        lm_srun._log = mocked_logger

        lm_cfg  = {'pre_exec': ['/bin/sleep']}
        env     = {'test_env': 'test_value'}
        env_sh  = 'env/lm_%s.sh' % lm_srun.name.lower()

        lm_info = lm_srun._init_from_scratch(lm_cfg, env, env_sh)
        self.assertEqual(lm_info, {'env'    : env,
                                   'env_sh' : env_sh,
                                   'command': mocked_which()})
        self.assertEqual(lm_srun._version, mocked_sh_callout()[0])

    # --------------------------------------------------------------------------
    #
    @mock.patch.object(Srun, '__init__', return_value=None)
    @mock.patch('radical.utils.which', return_value='/bin/srun')
    @mock.patch('radical.utils.sh_callout', return_value=['', 'error', 1])
    def test_init_from_scratch_fail(self, mocked_sh_callout,
                                    mocked_which, mocked_init):

        lm_srun = Srun(name=None, lm_cfg={}, cfg={}, log=None, prof=None)
        with self.assertRaises(RuntimeError):
            # error while getting version of the launch command
            lm_srun._init_from_scratch({}, {}, '')

    # --------------------------------------------------------------------------
    #
    @mock.patch.object(Srun, '__init__', return_value=None)
    def test_init_from_info(self, mocked_init):

        lm_srun = Srun(name=None, lm_cfg={}, cfg={}, log=None, prof=None)

        lm_info = {'env'    : {'test_env': 'test_value'},
                   'env_sh' : 'env/lm_srun.sh',
                   'command': '/bin/srun'}
        lm_srun._init_from_info(lm_info, {})
        self.assertEqual(lm_srun._env,     lm_info['env'])
        self.assertEqual(lm_srun._env_sh,  lm_info['env_sh'])
        self.assertEqual(lm_srun._command, lm_info['command'])

        lm_info['command'] = ''
        with self.assertRaises(AssertionError):
            lm_srun._init_from_info(lm_info, {})

    # --------------------------------------------------------------------------
    #
    @mock.patch.object(Srun, '__init__', return_value=None)
    def test_can_launch(self, mocked_init):

        lm_srun = Srun(name=None, lm_cfg={}, cfg={}, log=None, prof=None)
        self.assertTrue(lm_srun.can_launch(task=None))

    # --------------------------------------------------------------------------
    #
    @mock.patch.object(Srun, '__init__', return_value=None)
    def test_get_launch_cmds(self, mocked_init):

        lm_srun = Srun(name=None, lm_cfg={}, cfg={}, log=None, prof=None)
        lm_srun._cfg     = {}
        lm_srun._command = '/bin/srun'

        test_cases = setUp('lm', 'srun')
        for task, result in test_cases:
            if result != 'RuntimeError':
                command = lm_srun.get_launch_cmds(task, '')
                self.assertEqual(command, result, msg=task['uid'])

        # TODO: set test with `slots`


if __name__ == '__main__':

    tc = TestSrun()
    tc.test_init_from_scratch()
    tc.test_init_from_scratch_fail()
    tc.test_init_from_info()
    tc.test_can_launch()
    tc.test_get_launch_cmds()


# ------------------------------------------------------------------------------
# pylint: enable=protected-access, unused-argument, no-value-for-parameter
