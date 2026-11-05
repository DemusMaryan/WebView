"""The privileged lifecycle delegates proxy reload to its credential owner."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from mtw import control


class NginxOwnerTests(unittest.TestCase):
    def test_root_render_drops_privileges_without_relaxing_tls_checks(self):
        data=MagicMock()
        data.stat.return_value=SimpleNamespace(st_uid=1000,st_gid=1000)
        with patch.object(control,'DATA',data),patch.object(control.os,'getuid',return_value=0), \
                patch.object(control,'command') as command:
            control.write_nginx(add_server='compute-3',exclude=())
        argv=command.call_args.args[0];options=command.call_args.kwargs
        self.assertEqual(json.loads(argv[-1]),{'add_server':'compute-3','exclude':[]})
        self.assertEqual((options['user'],options['group'],options['extra_groups']),(1000,1000,[]))
        self.assertEqual(options['timeout'],30)
        self.assertEqual(options['env']['MTW_CONFIG'],str(control.CONFIG_PATH))

    def test_owner_render_failure_is_propagated(self):
        data=MagicMock()
        data.stat.return_value=SimpleNamespace(st_uid=1000,st_gid=1000)
        with patch.object(control,'DATA',data),patch.object(control.os,'getuid',return_value=0), \
                patch.object(control,'command',side_effect=RuntimeError('bad TLS')):
            with self.assertRaisesRegex(RuntimeError,'bad TLS'):
                control.write_nginx()
