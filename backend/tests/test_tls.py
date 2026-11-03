"""Real OpenSSL files in disposable native directories; no system trust changes."""
import hashlib
import os
import secrets
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from mtw import tls
from mtw.config import CONFIG, CONFIG_PATH, DATA, REPO


class LocalTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix="tls-tests-", dir=DATA / "reports")
        cls.root = Path(cls.temporary.name)
        cls.template = cls.root / "template"
        with patch.object(tls, "ROOT", cls.template):
            tls.setup()

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.temporary_case = tempfile.TemporaryDirectory(dir=self.root)
        self.addCleanup(self.temporary_case.cleanup)
        self.directory = Path(self.temporary_case.name) / "tls space"
        shutil.copytree(self.template, self.directory)
        self.enterContext(patch.object(tls, "ROOT", self.directory))

    def hashes(self):
        return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in tls.paths().items()}

    def test_repeat_setup_preserves_ca_and_leaf(self):
        before = self.hashes()
        result = tls.setup()
        self.assertFalse(result["new_ca"])
        self.assertFalse(result["server_issued"])
        self.assertEqual(before, self.hashes())
        self.assertIn("DNS:localhost", result["san"])
        self.assertIn("IP Address:127.0.0.1", result["san"])

    def test_explicit_leaf_renewal_keeps_ca_identity(self):
        before = self.hashes()
        result = tls.setup(renew_server=True)
        after = self.hashes()
        self.assertFalse(result["new_ca"])
        self.assertTrue(result["server_issued"])
        self.assertEqual(before["ca_cert"], after["ca_cert"])
        self.assertEqual(before["ca_key"], after["ca_key"])
        self.assertNotEqual(before["server_cert"], after["server_cert"])

    def test_missing_certificate_blocks_start_before_services(self):
        from mtw import control
        tls.paths()["server_cert"].unlink()
        with patch.object(control, "pg_start", side_effect=AssertionError("No services may start")), \
                patch.object(control, "launch", side_effect=AssertionError("No launch")):
            with self.assertRaisesRegex(ValueError, "missing"):
                control.start(["api", "nginx"])

    def test_mismatched_key_rejected(self):
        p = tls.paths()
        original = p["server_key"].read_bytes()
        shutil.copyfile(p["ca_key"], p["server_key"])
        with self.assertRaisesRegex(ValueError, "Invalid/expired/mismatched"):
            tls.validate()
        p["server_key"].write_bytes(original)
        encrypted = self.directory / "encrypted.key"
        result = subprocess.run(["openssl", "pkey", "-in", str(p["server_key"]), "-aes-256-cbc", "-passout", "stdin", "-out", str(encrypted)],
            input=secrets.token_urlsafe(32), text=True, capture_output=True, timeout=10, umask=0o077)
        self.assertEqual(result.returncode, 0)
        shutil.copyfile(encrypted, p["server_key"])
        with self.assertRaisesRegex(ValueError, "Invalid/expired/mismatched"):
            tls.validate()  # Must fail without an interactive OpenSSL password prompt.

    def test_private_key_permissions_rejected(self):
        tls.paths()["server_key"].chmod(0o644)
        with self.assertRaisesRegex(ValueError, "0600"):
            tls.validate()

    def test_incomplete_ca_is_not_replaced(self):
        before = tls.paths()["ca_cert"].read_bytes()
        tls.paths()["ca_key"].unlink()
        with self.assertRaisesRegex(ValueError, "Incomplete local CA"):
            tls.setup()
        self.assertEqual(before, tls.paths()["ca_cert"].read_bytes())

    def issue_bad_leaf(self, san, days):
        p = tls.paths()
        csr = self.directory / "bad.csr"
        ext = self.directory / "bad.ext"
        ext.write_text(f"basicConstraints=critical,CA:FALSE\nextendedKeyUsage=serverAuth\nsubjectAltName={san}\n")
        tls.openssl("req", "-new", "-key", p["server_key"], "-out", csr, "-subj", "/CN=localhost")
        tls.openssl("x509", "-req", "-in", csr, "-CA", p["ca_cert"], "-CAkey", p["ca_key"],
                    "-set_serial", "123456", "-days", days, "-out", p["server_cert"], "-extfile", ext)

    def test_wrong_san_rejected(self):
        self.issue_bad_leaf("DNS:wrong.invalid", "10")
        with self.assertRaisesRegex(ValueError, "Invalid/expired/mismatched"):
            tls.validate()

    def test_expired_leaf_rejected(self):
        self.issue_bad_leaf("DNS:localhost,IP:127.0.0.1", "-1")
        with self.assertRaisesRegex(ValueError, "Invalid/expired/mismatched"):
            tls.validate()

    def test_start_cli_missing_tls_has_clear_error_and_no_http_fallback(self):
        root = self.directory.parent / "empty-runtime"
        root.mkdir()
        config = CONFIG_PATH.read_text().replace(f'data_root = "{CONFIG["paths"]["data_root"]}"', f'data_root = "{root}"')
        path = root / "config.toml"
        path.write_text(config)
        result = subprocess.run(["bash", str(REPO / "scripts/start"), "api"],
            env=os.environ | {"MTW_CONFIG": str(path)}, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TLS", result.stderr)
        self.assertIn("setup-https", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(list((root / "pids").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
