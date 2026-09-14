import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.notifications.pc import protected_bytes, status, run


class PcPushTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_round_trip_never_stores_plaintext(self):
        plaintext = b'isolated-test-key-not-a-real-secret'
        protected = protected_bytes(plaintext)
        self.assertNotIn(plaintext, protected)
        self.assertEqual(protected_bytes(protected, decrypt=True), plaintext)
        with self.assertRaises(RuntimeError):
            protected_bytes(b'not-dpapi-data', decrypt=True)

    def test_explicit_disable_does_not_call_remote_or_sender(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'disabled').write_text('user stop')
            with patch('src.notifications.pc.GitHub') as github, patch('src.notifications.pc.invoke_sender') as sender:
                run({'target_repo': 'owner/schedule'}, root, once=True)
                sender.assert_not_called()
                github.return_value.head.assert_not_called()
            self.assertTrue(status(root)['disabled'])
            self.assertFalse(status(root)['running'])

    def test_sender_failure_is_retryable_without_logging_private_exception(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch('src.notifications.pc.GitHub') as github, patch('src.notifications.pc.invoke_sender', side_effect=RuntimeError('private endpoint')):
                github.return_value.head.return_value = 'revision'
                run({'target_repo': 'owner/schedule'}, root, once=True)
            text = (root / 'status.json').read_text()
            self.assertNotIn('private endpoint', text)
            self.assertFalse(json.loads(text)['healthy'])


if __name__ == '__main__':
    unittest.main()
