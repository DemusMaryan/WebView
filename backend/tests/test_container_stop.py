"""Never signal a reused namespace/PID belonging to an unrelated process."""
import unittest
from unittest.mock import patch
from mtw import container_runtime as runtime


class ContainerStopOwnershipTests(unittest.TestCase):
    def test_bootstrap_descendants_are_one_launch_but_independent_peers_are_two(self):
        parents={100:1,101:100,102:101,200:1,1:0}
        candidates=[{'pid':100},{'pid':102},{'pid':200}]
        self.assertEqual(runtime.outer_launches(candidates,parents.get),[{'pid':100},{'pid':200}])

    def test_namespace_without_owned_supervisor_is_not_signalled(self):
        with patch.object(runtime,'require_root'),patch.object(runtime,'namespace_members',return_value=[
            {'host_pid':123,'start_ticks':'999','container_pid':1}]),patch.object(runtime.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'Cannot prove ownership'):
                runtime.stop(None,'pid:[reused]')
            kill.assert_not_called()

    def test_supervisor_pid_reused_after_shutdown_is_not_signalled_again(self):
        old={'pid':123,'start_ticks':'999','argv':'owned'}
        new=old|{'start_ticks':'1000','argv':'unrelated'}
        with patch.object(runtime,'require_root'),patch.object(runtime,'identity',side_effect=[old,None,new]), \
            patch.object(runtime,'owned_launches',return_value=[old]),patch.object(runtime,'namespace_of_supervisor',return_value=None), \
            patch.object(runtime,'namespace_members',return_value=[]),patch.object(runtime.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'reused during stop'):
                runtime.stop(old)
            kill.assert_called_once_with(123,runtime.signal.SIGTERM)

    def test_owned_supervisor_cannot_authorize_a_different_namespace(self):
        info={'pid':123,'start_ticks':'999','argv':'owned'}
        with patch.object(runtime,'require_root'),patch.object(runtime,'identity',return_value=info), \
            patch.object(runtime,'owned_launches',return_value=[info]),patch.object(runtime,'namespace_of_supervisor',return_value='pid:[actual]'), \
            patch.object(runtime.os,'kill') as kill:
            with self.assertRaisesRegex(RuntimeError,'does not belong'):
                runtime.stop(info,'pid:[different]')
            kill.assert_not_called()
