"""Connection diagnostics use synthetic tokens, a temporary profile and fake APIs."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.school import connect, sources


TOKEN = 'SYNTHETIC_TOKEN_DO_NOT_LOG_123'
PRIVATE = 'PRIVATE_PROFILE_OR_RESPONSE_DO_NOT_LOG'
ENCRYPTED = b'dpapi-synthetic-ciphertext'
CONFIG = {'version': 1, 'term': {'start': '2026-09-01', 'end': '2026-12-31'},
          'etl': {'base_url': 'https://myetl.snu.ac.kr'},
          'courses': [{'key': 'logic', 'name': '논리설계 및 실험', 'aliases': ['논설']}]}
VALID = {'status': 'ok', 'issues': []}
MATCHED_EMPTY = {'status': 'ok', 'courses': [{'key': 'logic', 'canvas_id': '123'}], 'sources': [], 'issues': []}
MISMATCH = {'status': 'error', 'courses': [], 'sources': [],
            'issues': [{'code': 'course_not_found_or_ambiguous', 'course': 'logic'}]}


class SchoolConnectTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / 'ScheduleSchool'
        self.root.mkdir()
        (self.root / 'sources.json').write_text(json.dumps(CONFIG), encoding='utf-8')
        self.environment = patch.dict(os.environ, {'LOCALAPPDATA': self.directory.name})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def invoke(self, *, probe=None, collection=None, cloud=False, private=True, command=None, token=TOKEN):
        self.stdout, self.stderr = io.StringIO(), io.StringIO()
        self.github = Mock()
        self.github.api.return_value = {'private': private}
        with patch.object(connect.getpass, 'getpass', return_value=token), \
             patch.object(connect, 'probe_etl', create=True) as self.probe, \
             patch.object(connect, 'collect_etl') as self.collect, \
             patch.object(connect, 'protected_bytes', return_value=ENCRYPTED) as self.protect, \
             patch.object(connect, 'GitHub', return_value=self.github), \
             patch.object(connect.subprocess, 'run') as self.command, \
             redirect_stdout(self.stdout), redirect_stderr(self.stderr):
            self.probe.side_effect = probe if isinstance(probe, Exception) else None
            self.probe.return_value = deepcopy(VALID if probe is None else probe)
            self.collect.side_effect = collection if isinstance(collection, Exception) else None
            self.collect.return_value = deepcopy(MATCHED_EMPTY if collection is None else collection)
            self.command.side_effect = command if isinstance(command, Exception) else None
            self.command.return_value = command or SimpleNamespace(returncode=0, stdout=b'', stderr=b'')
            result = connect.main(['--cloud'] if cloud else [])
        output = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertNotIn(TOKEN, output)
        self.assertNotIn(PRIVATE, output)
        self.assertNotIn('https://private.invalid', output)
        return result, output

    def diagnostic(self):
        value = json.loads((self.root / 'diagnostic.json').read_text(encoding='utf-8'))
        encoded = json.dumps(value)
        self.assertNotIn(TOKEN, encoded)
        self.assertNotIn(PRIVATE, encoded)
        self.assertNotIn('https://private.invalid', encoded)
        return value

    def test_identity_success_saves_valid_token_despite_no_course_matches(self):
        code, output = self.invoke(collection=MISMATCH)
        self.assertEqual(code, 0)
        self.probe.assert_called_once()
        self.collect.assert_called_once()
        self.protect.assert_called_once_with(TOKEN.encode('utf-8'))
        self.assertEqual((self.root / 'etl.dpapi').read_bytes(), ENCRYPTED)
        self.assertTrue((self.root / 'collect.request').is_file())
        self.assertIn('course_not_found_or_ambiguous', json.dumps(self.diagnostic()))
        self.assertIn('course_not_found_or_ambiguous', output)
        self.command.assert_not_called()

    def test_valid_identity_and_matched_course_with_zero_assignments_is_successful(self):
        code, _ = self.invoke(collection=MATCHED_EMPTY)
        self.assertEqual(code, 0)
        self.assertEqual((self.root / 'etl.dpapi').read_bytes(), ENCRYPTED)
        self.diagnostic()

    def test_authentication_failure_preserves_existing_token_and_never_collects_or_uploads(self):
        (self.root / 'etl.dpapi').write_bytes(b'previous-encrypted-token')
        code, output = self.invoke(probe={'status': 'auth_required', 'issues': [{'code': 'auth_required'}]}, cloud=True)
        self.assertNotEqual(code, 0)
        self.assertEqual((self.root / 'etl.dpapi').read_bytes(), b'previous-encrypted-token')
        self.assertFalse((self.root / 'collect.request').exists())
        self.protect.assert_not_called()
        self.collect.assert_not_called()
        self.command.assert_not_called()
        self.assertIn('auth_required', output)
        self.assertIn('auth_required', json.dumps(self.diagnostic()))

    def test_cloud_upload_uses_stdin_only_after_identity_validation_and_private_repo_check(self):
        code, _ = self.invoke(cloud=True, collection=MISMATCH)
        self.assertEqual(code, 0)
        self.github.api.assert_called_once_with('repos/jdyece25-byte/schedule-requests')
        self.command.assert_called_once()
        args, kwargs = self.command.call_args
        self.assertEqual(args[0][:4], ['gh', 'secret', 'set', 'ETL_API_TOKEN'])
        self.assertNotIn(TOKEN, ' '.join(args[0]))
        self.assertEqual(kwargs['input'], TOKEN.encode('utf-8'))
        self.assertTrue(kwargs['capture_output'])

    def test_public_destination_never_receives_token_but_local_auth_is_preserved(self):
        code, _ = self.invoke(cloud=True, private=False)
        self.assertNotEqual(code, 0)
        self.command.assert_not_called()
        self.assertEqual((self.root / 'etl.dpapi').read_bytes(), ENCRYPTED)

    def test_cloud_command_failure_never_echoes_provider_output_or_token(self):
        failed = SimpleNamespace(returncode=1, stdout=(TOKEN + PRIVATE).encode(), stderr=b'https://private.invalid/error')
        code, _ = self.invoke(cloud=True, command=failed)
        self.assertNotEqual(code, 0)
        self.assertEqual((self.root / 'etl.dpapi').read_bytes(), ENCRYPTED)

    def test_unexpected_probe_failure_is_redacted_and_does_not_store_token(self):
        code, _ = self.invoke(probe=RuntimeError(TOKEN + PRIVATE))
        self.assertNotEqual(code, 0)
        self.assertFalse((self.root / 'etl.dpapi').exists())
        self.protect.assert_not_called()
        self.command.assert_not_called()
        self.diagnostic()

    def test_collection_exception_does_not_invalidate_a_separately_verified_token(self):
        code, _ = self.invoke(collection=RuntimeError(TOKEN + PRIVATE))
        self.assertEqual(code, 0)
        self.assertEqual((self.root / 'etl.dpapi').read_bytes(), ENCRYPTED)
        self.diagnostic()

    def test_cloud_exception_is_redacted_after_local_success(self):
        code, _ = self.invoke(cloud=True, command=RuntimeError(TOKEN + PRIVATE))
        self.assertNotEqual(code, 0)
        self.assertEqual((self.root / 'etl.dpapi').read_bytes(), ENCRYPTED)

    def test_sanitized_diagnostic_and_description_discard_untrusted_nested_data(self):
        probe = {**VALID, 'profile': {'name': PRIVATE, 'email': TOKEN}}
        collection = deepcopy(MISMATCH)
        collection.update(sources=[{'title': PRIVATE, 'content': TOKEN, 'source_url': 'https://private.invalid/source'}],
                          raw_profile=PRIVATE, token=TOKEN)
        collection['issues'] += [{'code': TOKEN, 'message': PRIVATE, 'url': 'https://private.invalid/error'},
                                 {'code': 'api_unavailable', 'profile': {'name': PRIVATE}, 'course': TOKEN}]
        diagnostic = connect.safe_diagnostic(probe, collection)
        text = json.dumps(diagnostic) + connect.describe(diagnostic)
        for forbidden in (TOKEN, PRIVATE, 'https://private.invalid'):
            self.assertNotIn(forbidden, text)
        self.assertIn('course_not_found_or_ambiguous', text)

    def test_blank_input_never_touches_existing_credential(self):
        (self.root / 'etl.dpapi').write_bytes(b'previous-encrypted-token')
        code, _ = self.invoke(token='   ')
        self.assertNotEqual(code, 0)
        self.probe.assert_not_called()
        self.collect.assert_not_called()
        self.protect.assert_not_called()
        self.command.assert_not_called()
        self.assertEqual((self.root / 'etl.dpapi').read_bytes(), b'previous-encrypted-token')


class IdentityProbeTests(unittest.TestCase):
    def test_valid_profile_only_returns_sanitized_auth_status(self):
        fetch = Mock(return_value=({'id': 42, 'name': PRIVATE, 'primary_email': TOKEN,
                                   'calendar': {'ics': 'https://private.invalid/calendar'}}, {}))
        result = sources.probe_etl(CONFIG, TOKEN, fetch=fetch)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['issues'], [])
        self.assertEqual(set(result), {'status', 'issues'})
        args, _ = fetch.call_args
        self.assertEqual(args[0], 'https://myetl.snu.ac.kr/api/v1/users/self/profile')
        self.assertEqual(args[1]['Authorization'], 'Bearer ' + TOKEN)
        self.assertNotIn(TOKEN, args[0])
        self.assertNotIn(PRIVATE, json.dumps(result))

    def test_invalid_profile_shape_cannot_authenticate(self):
        for value in ({}, None, [], {'id': 0}, {'id': -1}, {'id': True}, {'id': False}, {'id': 42.5}):
            with self.subTest(value=value):
                result = sources.probe_etl(CONFIG, TOKEN, fetch=Mock(return_value=(value, {})))
                self.assertNotEqual(result['status'], 'ok')

    def test_auth_and_transport_errors_return_reason_codes_without_exception_text(self):
        for error in (sources.CollectionError('auth_required'), RuntimeError(TOKEN + PRIVATE)):
            result = sources.probe_etl(CONFIG, TOKEN, fetch=Mock(side_effect=error))
            self.assertNotEqual(result['status'], 'ok')
            self.assertNotIn(TOKEN, json.dumps(result))
            self.assertNotIn(PRIVATE, json.dumps(result))

    def test_unsafe_origin_is_rejected_before_sending_auth_header(self):
        fetch = Mock()
        result = sources.probe_etl({**CONFIG, 'etl': {'base_url': 'https://private.invalid/'}}, TOKEN, fetch=fetch)
        self.assertNotEqual(result['status'], 'ok')
        fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
