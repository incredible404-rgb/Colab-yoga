"""Offline regressions for temporary Google credential refresh failures."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from google.auth.exceptions import RefreshError, TransportError
import colab_yoga_bridge as cb


class AuthRefreshFailureTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.token = Path(directory.name) / 'token.json'
        self.token.write_text('{}', encoding='utf-8')
        self.creds = mock.Mock(valid=False, expired=True, refresh_token='synthetic-refresh')
        self.creds.to_json.return_value = '{"token":"synthetic-access"}'
        for patcher in (
            mock.patch.object(cb, 'TOKEN_CONFIG_PATH', str(self.token)),
            mock.patch.object(cb.Credentials, 'from_authorized_user_file', return_value=self.creds),
            mock.patch.object(cb.time, 'sleep'),
            mock.patch.object(cb, '_CHECKED_CREDS', None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def status(self, command=cb.cmd_auth_status):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            command()
        return json.loads(output.getvalue())

    def test_transient_refresh_is_retried_and_credentials_are_kept(self):
        self.creds.refresh.side_effect = [TransportError('temporary network failure'), None]
        self.assertTrue(cb._has_valid_token())
        self.assertEqual(self.creds.refresh.call_count, 2)
        self.assertIs(cb._CHECKED_CREDS, self.creds)
        self.assertEqual(json.loads(self.token.read_text()), {'token': 'synthetic-access'})

    def test_retryable_google_server_error_is_retried(self):
        self.creds.refresh.side_effect = [RefreshError('server error', retryable=True), None]
        self.assertTrue(cb._has_valid_token())
        self.assertEqual(self.creds.refresh.call_count, 2)

    def test_persistent_transport_error_keeps_login_unknown_and_token_unchanged(self):
        self.creds.refresh.side_effect = TransportError('sensitive exception details')
        body = self.status()
        self.assertFalse(body['ok'])
        self.assertTrue(body['retryable'])
        self.assertIsNot(body.get('logged_in'), False)
        self.assertIn('网络', body['error'])
        self.assertNotIn('sensitive', json.dumps(body))
        self.assertEqual(self.creds.refresh.call_count, 3)
        self.assertEqual(self.token.read_text(), '{}')

    def test_revoked_refresh_token_still_reports_logged_out_without_retry(self):
        self.creds.refresh.side_effect = RefreshError('revoked', {'error': 'invalid_grant'})
        self.assertEqual(self.status(), {'ok': True, 'logged_in': False})
        self.assertEqual(self.creds.refresh.call_count, 1)

    def test_client_configuration_error_does_not_claim_the_account_is_logged_out(self):
        self.creds.refresh.side_effect = RefreshError('bad client', {'error': 'invalid_client'})
        body = self.status()
        self.assertFalse(body['ok'])
        self.assertFalse(body['retryable'])
        self.assertIsNot(body.get('logged_in'), False)
        self.assertEqual(self.creds.refresh.call_count, 1)

    def test_corrupt_credentials_offer_rebinding_without_deleting_the_file(self):
        with mock.patch.object(cb.Credentials, 'from_authorized_user_file', side_effect=ValueError('bad json')):
            body = self.status()
        self.assertFalse(body['ok'])
        self.assertTrue(body['reauth_required'])
        self.assertEqual(self.token.read_text(), '{}')

    def test_client_mismatch_offers_rebinding(self):
        self.creds.refresh.side_effect = RefreshError('bad client', {'error': 'unauthorized_client'})
        body = self.status()
        self.assertTrue(body['reauth_required'])
        self.assertFalse(body['retryable'])

    def test_keeper_records_unavailable_instead_of_logged_out_and_releases_nothing(self):
        self.creds.refresh.side_effect = TransportError('offline')
        snapshots = []
        with mock.patch.object(cb, 'keeper_status', return_value={'alive': False}), \
                mock.patch.object(cb, '_keeper_save', side_effect=lambda value: snapshots.append(dict(value))), \
                mock.patch.object(cb, 'state') as state:
            cb.cmd_keeper()
        self.assertEqual(snapshots[-1]['exit_reason'], 'auth_unavailable')
        self.assertIn('网络', snapshots[-1]['last_detail'])
        state.client.unassign.assert_not_called()
        self.assertEqual(self.token.read_text(), '{}')

    def test_wake_returns_network_error_before_any_allocation(self):
        self.creds.refresh.side_effect = TransportError('offline')
        output = io.StringIO()
        with mock.patch.object(cb, '_allocate_fresh_session') as allocate, \
                contextlib.redirect_stdout(output):
            self.assertFalse(cb.cmd_wake())
        allocate.assert_not_called()
        self.assertIn('WAKE_ERROR:', output.getvalue())
        self.assertIn('网络', output.getvalue())
        self.assertNotIn('绑定账号', output.getvalue())

    def test_stop_and_discover_preserve_login_on_transport_error(self):
        self.creds.refresh.side_effect = TransportError('offline')
        for command in (cb.cmd_stop_all, cb.cmd_discover_url):
            with self.subTest(command=command.__name__), mock.patch.object(cb, 'state') as state:
                body = self.status(command)
                self.assertFalse(body['ok'])
                self.assertTrue(body['retryable'])
                self.assertIsNot(body.get('logged_in'), False)
                state.client.unassign.assert_not_called()


class KeeperHealthCodeTests(unittest.TestCase):
    def test_keeper_reads_only_public_health_and_keeps_idle_release_information(self):
        health = {'ok': True, 'idle_remaining_seconds': 12, 'idle_timeout_minutes': 30,
                  'is_downloading': False, 'is_loading_vram': False, 'llm_online': True}
        opener = mock.Mock()
        opener.open.return_value = io.BytesIO(json.dumps(health).encode())
        namespace = {}
        output = io.StringIO()
        with mock.patch('urllib.request.build_opener', return_value=opener), \
                mock.patch.object(cb.os.path, 'exists', return_value=False), \
                mock.patch.object(cb.subprocess, 'run', return_value=mock.Mock(returncode=0)), \
                contextlib.redirect_stdout(output):
            exec(compile(cb._KEEPER_CODE, '<keeper-code>', 'exec'), namespace)
        self.assertEqual(opener.open.call_args.args[0], 'http://127.0.0.1:7860/health')
        result = json.loads(output.getvalue().removeprefix('EC_KEEPER:'))
        self.assertEqual((result['idle_remaining'], result['idle_timeout'], result['llm_online']), (12, 30, True))


if __name__ == '__main__':
    unittest.main()
