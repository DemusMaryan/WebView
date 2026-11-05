"""A scanner rule string is not a PEM certificate payload."""
import base64
import importlib.util
from pathlib import Path
import re
import unittest

spec=importlib.util.spec_from_file_location('publication_audit',Path(__file__).resolve().parents[2]/'scripts/audit-publication.py')
audit=importlib.util.module_from_spec(spec);spec.loader.exec_module(audit)


class PublicationAuditTests(unittest.TestCase):
    def test_header_literal_in_scanner_source_is_not_a_certificate(self):
        source=Path(audit.__file__).read_bytes()
        self.assertIsNone(re.search(audit.CERTIFICATE_PAYLOAD,source))
        self.assertIsNone(re.search(audit.CERTIFICATE_PAYLOAD,b"rule=rb'-----BEGIN CERTIFICATE-----'"))

    def test_multiline_base64_payload_is_detected(self):
        # Runtime-generated scanner fixture, not a real certificate or key.
        payload=b'-----BEGIN CERTIFICATE-----'+b'\r\n'+base64.b64encode(bytes(range(128)))+b'\r\n-----END CERTIFICATE-----'
        self.assertIsNotNone(re.search(audit.CERTIFICATE_PAYLOAD,payload))
