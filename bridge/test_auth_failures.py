"""Offline regressions for temporary Google credential refresh failures."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

# The bundled Windows runtime does not put the script's own directory on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from google.auth.exceptions import RefreshError, TransportError
import keyring
from keyring.backend import KeyringBackend
from keyring.errors import KeyringError, PasswordDeleteError
import colab_yoga_bridge as cb


class MemoryKeyring(KeyringBackend):
    """In-process keyring: no test may reach Windows Credential Manager or the macOS Keychain."""
    priority = 1

    def __init__(self):
        super().__init__()
        self.reset()

    def reset(self):
        self.data = {}
        self.fail_writes = False
        self.fail_all = None  # an exception every call raises, like a backend that cannot reach its store
        self.deny_delete = False  # the macOS backend's PasswordDeleteError for an entry it could not delete

    def get_password(self, service, username):
        if self.fail_all:
            raise self.fail_all
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        if self.fail_all:
            raise self.fail_all
        if self.fail_writes:
            raise KeyringError('locked')
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        if self.fail_all:
            raise self.fail_all
        if self.deny_delete or self.data.pop((service, username), None) is None:
            raise PasswordDeleteError('missing or denied')


class WinCredError(Exception):
    """Stands in for win32ctypes' pywintypes.error: not KeyringError, not OSError."""


_RING = None


def isolate_token_store() -> MemoryKeyring:
    """Point keyring and both legacy token.json paths (the bridge's and colab-cli's own) away from
    the real ones, once per process: a real ~/.config/colab-cli/token.json would otherwise be
    migrated and deleted by a test. Every caller gets the same keyring."""
    global _RING
    if _RING is None:
        _RING = MemoryKeyring()
        keyring.set_keyring(_RING)
        legacy = str(Path(tempfile.mkdtemp()) / 'token.json')
        cb.TokenStore.legacy_path = legacy
        cb._cli_auth.LEGACY_TOKEN_PATH = legacy
    return _RING


RING = isolate_token_store()
KEY = (cb.TokenStore.service, cb.TokenStore.username)


class TokenStoreTests(unittest.TestCase):
    def setUp(self):
        RING.reset()
        self.addCleanup(RING.reset)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.legacy = Path(directory.name) / 'token.json'
        self.store = cb.TokenStore()
        self.store.legacy_path = str(self.legacy)

    def test_nothing_saved_reads_none(self):
        self.assertIsNone(self.store.read())

    def test_legacy_file_moves_into_the_keyring_and_is_deleted(self):
        self.legacy.write_text('{"refresh_token": "r"}', encoding='utf-8')
        self.assertEqual(self.store.read(), '{"refresh_token": "r"}')
        self.assertEqual(RING.data[KEY], '{"refresh_token": "r"}')
        self.assertFalse(self.legacy.exists())

    def test_legacy_file_is_kept_and_used_when_the_keyring_refuses_the_write(self):
        self.legacy.write_text('{"refresh_token": "r"}', encoding='utf-8')
        RING.fail_writes = True
        self.assertEqual(self.store.read(), '{"refresh_token": "r"}')
        self.assertTrue(self.legacy.exists())

    def test_corrupt_legacy_file_is_left_alone(self):
        self.legacy.write_text('not json', encoding='utf-8')
        with self.assertRaises(ValueError):
            self.store.read()
        self.assertTrue(self.legacy.exists())
        self.assertNotIn(KEY, RING.data)

    def test_keyring_wins_and_a_stale_legacy_file_is_removed(self):
        RING.data[KEY] = '{"refresh_token": "new"}'
        self.legacy.write_text('{"refresh_token": "old"}', encoding='utf-8')
        self.assertEqual(self.store.read(), '{"refresh_token": "new"}')
        self.assertFalse(self.legacy.exists())

    def test_the_same_entry_as_the_colab_cli(self):
        self.store.write('{"t": 1}')
        self.assertEqual(keyring.get_password(cb._cli_auth.KEYRING_SERVICE_NAME, cb._cli_auth.KEYRING_USERNAME),
                         '{"t": 1}')

    def test_delete_removes_both_and_tolerates_nothing_saved(self):
        self.store.write('{"t": 1}')
        self.legacy.write_text('{}', encoding='utf-8')
        self.store.delete()
        self.store.delete()
        self.assertNotIn(KEY, RING.data)
        self.assertFalse(self.legacy.exists())

    def test_delete_that_left_the_token_in_place_is_an_error(self):
        self.store.write('{"t": 1}')
        RING.deny_delete = True
        with self.assertRaises(PasswordDeleteError):
            self.store.delete()
        self.assertEqual(RING.data[KEY], '{"t": 1}')

    def test_any_backend_exception_becomes_keyring_error(self):
        RING.fail_all = WinCredError(1312, 'CredRead', 'A specified logon session does not exist.')
        for call in (self.store.read, lambda: self.store.write('{}'), self.store.delete):
            with self.subTest(call=call), self.assertRaises(KeyringError):
                call()

    def test_unreadable_keyring_entry_gives_way_to_a_usable_legacy_file(self):
        RING.data[KEY] = 'not json'
        self.legacy.write_text('{"refresh_token": "file"}', encoding='utf-8')
        self.assertEqual(self.store.read(), '{"refresh_token": "file"}')
        self.assertEqual(RING.data[KEY], '{"refresh_token": "file"}')
        self.assertFalse(self.legacy.exists())

    def test_unreadable_keyring_entry_without_a_file_is_a_value_error(self):
        RING.data[KEY] = 'not json'
        with self.assertRaises(ValueError):
            self.store.read()

    def test_a_legacy_file_that_cannot_be_removed_does_not_fail_a_write(self):
        self.legacy.write_text('{}', encoding='utf-8')
        with mock.patch.object(cb.os, 'remove', side_effect=PermissionError('locked')):
            self.store.write('{"t": 2}')
        self.assertEqual(RING.data[KEY], '{"t": 2}')


class KeyringFailureCommandTests(unittest.TestCase):
    """A store that cannot be reached leaves the login state unknown, never "logged out"."""

    def setUp(self):
        RING.reset()
        self.addCleanup(RING.reset)
        RING.data[KEY] = '{"refresh_token": "r"}'
        RING.fail_all = WinCredError(1312, 'CredRead', 'no logon session')
        for patcher in (mock.patch.object(cb, 'TOKEN_STORE', cb.TokenStore()),
                        mock.patch.object(cb, '_CHECKED_CREDS', None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_json(self, command):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            command()
        return json.loads(output.getvalue())

    def test_auth_status_reports_unavailable(self):
        body = self.run_json(cb.cmd_auth_status)
        self.assertFalse(body['ok'])
        self.assertIsNot(body.get('logged_in'), False)

    def test_logout_that_cannot_delete_keeps_the_sessions(self):
        with mock.patch.object(cb, '_clear_local_session_store') as clear:
            body = self.run_json(cb.cmd_auth_logout)
        self.assertFalse(body['ok'])
        clear.assert_not_called()
        RING.fail_all = None
        self.assertIn(KEY, RING.data)

    def test_logout_denied_by_the_keychain_keeps_the_sessions(self):
        RING.fail_all = None
        RING.deny_delete = True
        with mock.patch.object(cb, '_clear_local_session_store') as clear:
            body = self.run_json(cb.cmd_auth_logout)
        self.assertFalse(body['ok'])
        clear.assert_not_called()


class MemoryTokenStore:
    def __init__(self, raw=None):
        self.raw = raw

    def read(self):
        return self.raw

    def write(self, raw):
        self.raw = raw

    def delete(self):
        self.raw = None


class AuthRefreshFailureTests(unittest.TestCase):
    def setUp(self):
        self.token = MemoryTokenStore('{}')
        self.creds = mock.Mock(valid=False, expired=True, refresh_token='synthetic-refresh')
        self.creds.to_json.return_value = '{"token":"synthetic-access"}'
        for patcher in (
            mock.patch.object(cb, 'TOKEN_STORE', self.token),
            mock.patch.object(cb.Credentials, 'from_authorized_user_info', return_value=self.creds),
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
        self.assertEqual(json.loads(self.token.raw), {'token': 'synthetic-access'})

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
        self.assertEqual(self.token.raw, '{}')

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
        with mock.patch.object(cb.Credentials, 'from_authorized_user_info', side_effect=ValueError('bad json')):
            body = self.status()
        self.assertFalse(body['ok'])
        self.assertTrue(body['reauth_required'])
        self.assertEqual(self.token.raw, '{}')

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
        self.assertEqual(self.token.raw, '{}')

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
