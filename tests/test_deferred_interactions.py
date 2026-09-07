import importlib
import json
import os
import pathlib
import sys
import time
import unittest
from unittest.mock import patch

from nacl.signing import SigningKey

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src' / 'app'))
os.environ.setdefault('AWS_EC2_METADATA_DISABLED', 'true')


class DeferredInteractionsTest(unittest.TestCase):
    def setUp(self):
        self.key = SigningKey.generate()
        os.environ['DISCORD_PUBLIC_KEY'] = self.key.verify_key.encode().hex()
        sys.modules.pop('main', None)
        self.main = importlib.import_module('main')

    def request(self, command, roles=None):
        payload = {
            'type': 2, 'id': '123', 'application_id': '456',
            'token': 'test-interaction-token', 'data': {'name': command},
            'member': {'roles': roles or []},
        }
        body = json.dumps(payload).encode()
        timestamp = str(int(time.time()))
        return self.main.app.test_client().post('/interactions', data=body, headers={
            'Content-Type': 'application/json',
            'X-Signature-Timestamp': timestamp,
            'X-Signature-Ed25519': self.key.sign(timestamp.encode() + body).signature.hex(),
        })

    def test_slow_aws_work_is_acknowledged_before_discord_deadline(self):
        events = []
        started = time.monotonic()

        def send(req, timeout):
            events.append((req.method, json.loads(req.data), time.monotonic() - started))
            from contextlib import nullcontext
            return nullcontext()

        def slow_status():
            time.sleep(3.1)
            return 'status result'

        with patch('urllib.request.urlopen', side_effect=send), patch.object(
            self.main, 'handle_status_dev', side_effect=slow_status
        ):
            response = self.request('status_dev')

        self.assertEqual(len(events), 2, 'Send a deferred callback before AWS work and edit it afterward')
        self.assertEqual(events[0][:2], ('POST', {'type': 5}))
        self.assertLess(events[0][2], 1)
        self.assertGreaterEqual(events[1][2], 3.1)
        self.assertEqual(events[1][0], 'PATCH')
        self.assertEqual(events[1][1]['content'], 'status result')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.data, b'')

    def test_command_failure_replaces_loading_with_an_error(self):
        requests = []
        from contextlib import nullcontext

        def send(req, timeout):
            requests.append(json.loads(req.data))
            return nullcontext()

        with patch('urllib.request.urlopen', side_effect=send), patch.object(
            self.main, 'handle_status_dev', side_effect=RuntimeError('test failure')
        ):
            response = self.request('status_dev')

        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(requests), 2)
        self.assertIn('오류', requests[1]['content'])
        self.assertNotIn('test failure', requests[1]['content'])

    def test_failed_acknowledgement_does_not_execute_command(self):
        from urllib.error import URLError
        with patch('urllib.request.urlopen', side_effect=URLError('unavailable')), patch.object(
            self.main, 'handle_start_dev'
        ) as start:
            response = self.request('start_dev', [self.main.ROLE_MAPPING['인프라']])
        start.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['type'], 4)
        self.assertIn('접수', response.get_json()['data']['content'])

    def test_temporary_reply_failure_retries_message_without_repeating_command(self):
        from contextlib import nullcontext
        from urllib.error import HTTPError
        failure = HTTPError('https://discord.com/private-token', 503, 'unavailable', {}, None)
        with patch('urllib.request.urlopen', side_effect=[nullcontext(), failure, nullcontext()]) as send, patch.object(
            self.main, 'handle_stop_dev', return_value='stopping'
        ) as stop, patch.object(self.main.time, 'sleep'):
            response = self.request('stop_dev', [self.main.ROLE_MAPPING['인프라']])
        self.assertEqual(response.status_code, 202)
        stop.assert_called_once_with(['인프라'])
        self.assertEqual([c.args[0].method for c in send.call_args_list], ['POST', 'PATCH', 'PATCH'])

    def test_start_roles_and_callback_urls_are_preserved(self):
        from contextlib import nullcontext
        with patch('urllib.request.urlopen', return_value=nullcontext()) as send, patch.object(
            self.main, 'handle_start_dev', return_value='starting'
        ) as start:
            response = self.request('start_dev', [self.main.ROLE_MAPPING['인프라'], 'unknown-role'])
        start.assert_called_once_with(['인프라'])
        self.assertEqual(response.status_code, 202)
        self.assertEqual(send.call_args_list[0].args[0].full_url,
                         'https://discord.com/api/v10/interactions/123/test-interaction-token/callback')
        self.assertEqual(send.call_args_list[1].args[0].full_url,
                         'https://discord.com/api/v10/webhooks/456/test-interaction-token/messages/@original')

    def test_invalid_signature_cannot_send_callback_or_execute_command(self):
        with patch('urllib.request.urlopen') as send, patch.object(self.main, 'handle_status_dev') as status:
            response = self.main.app.test_client().post('/interactions', json={
                'type': 2, 'data': {'name': 'status_dev'}, 'token': 'private-token',
            })
        self.assertEqual(response.status_code, 401)
        send.assert_not_called()
        status.assert_not_called()

    def test_expired_reply_does_not_retry_command_or_log_token(self):
        from contextlib import nullcontext, redirect_stdout
        from io import StringIO
        from urllib.error import HTTPError
        output = StringIO()
        failure = HTTPError('https://discord.com/private-token', 404, 'not found', {}, None)
        with redirect_stdout(output), patch('urllib.request.urlopen', side_effect=[nullcontext(), failure]) as send, patch.object(
            self.main, 'handle_stop_dev', return_value='stopped'
        ) as stop:
            response = self.request('stop_dev')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(send.call_count, 2)
        stop.assert_called_once()
        self.assertNotIn('test-interaction-token', output.getvalue())
        self.assertNotIn('private-token', output.getvalue())

    def test_reply_retry_exhaustion_is_bounded_without_repeating_command(self):
        from contextlib import nullcontext
        from urllib.error import URLError
        with patch('urllib.request.urlopen', side_effect=[nullcontext()] + [URLError('unavailable')] * 3) as send, patch.object(
            self.main, 'handle_status_dev', return_value='status'
        ) as status, patch.object(self.main.time, 'sleep'):
            response = self.request('status_dev')
        self.assertEqual(response.status_code, 202)
        self.assertEqual(send.call_count, 4)
        status.assert_called_once()

    def test_unauthorized_start_does_not_access_aws(self):
        from contextlib import nullcontext
        with patch('urllib.request.urlopen', return_value=nullcontext()) as send, patch.object(
            self.main, 'load_active_teams'
        ) as state, patch.object(self.main, 'start_dev_stack') as start:
            self.request('start_dev')
        state.assert_not_called()
        start.assert_not_called()
        self.assertIn('권한', json.loads(send.call_args_list[1].args[0].data)['content'])


if __name__ == '__main__':
    unittest.main()
