import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / '.build' / 'agent-screen'


def call(tool, arguments, request_id=1):
    return {'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
            'params': {'name': tool, 'arguments': arguments}}


class ProtocolTests(unittest.TestCase):
    def exchange(self, lines):
        payload = '\n'.join(line if isinstance(line, str) else json.dumps(line) for line in lines) + '\n'
        result = subprocess.run([str(BINARY), 'mcp'], input=payload, text=True,
                                capture_output=True, timeout=10, check=True)
        self.assertEqual(result.stderr, '')
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_catalogue_and_version_negotiation(self):
        replies = self.exchange([
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': 'unknown'}},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'ping'},
        ])
        self.assertEqual(replies[0]['result']['protocolVersion'], '2025-11-25')
        self.assertEqual(replies[0]['result']['serverInfo']['version'], '1.1.0')
        catalogue = replies[1]['result']['tools']
        self.assertEqual({tool['name'] for tool in catalogue},
                         {'agent_screen_prepare', 'agent_screen_status', 'agent_screen_move'})
        self.assertTrue(all(tool['inputSchema']['additionalProperties'] is False for tool in catalogue))
        self.assertEqual(replies[2]['result'], {})

    def test_catalogue_has_no_obsolete_single_screen_route(self):
        catalogue = self.exchange([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}])[0]['result']['tools']
        descriptions = {tool['name']: tool['description'] for tool in catalogue}
        for name in ['agent_screen_prepare', 'agent_screen_status']:
            self.assertIn('physical screen', descriptions[name])
            self.assertNotIn('use SpaceO MCP instead', descriptions[name])
        self.assertIn('no placement, SpaceO, isolation, or health check', descriptions['agent_screen_prepare'])
        self.assertIn('controller', descriptions['agent_screen_move'])

    def test_read_only_route_targets_a_connected_physical_screen(self):
        reply = self.exchange([call('agent_screen_prepare', {})])[0]['result']
        if reply.get('isError'):
            message = reply['content'][0]['text']
            if message in ['没有可用显示屏。', '显示屏处于镜像模式，请先改成扩展显示。']:
                self.skipTest('Extended physical display unavailable on this runner')
            self.fail(message)
        route = json.loads(reply['content'][0]['text'])
        screens = route['screens']
        multiple = len(screens) > 1
        target = screens[1 if multiple else 0]
        self.assertEqual(route['mode'], 'second_physical_screen' if multiple else 'single_physical_screen')
        self.assertEqual(route['target_display_id'], target['id'])
        self.assertEqual(route['target_screen'], target['name'])
        self.assertEqual(route['placement_required'], multiple)
        self.assertNotIn('next_tool', route)
        self.assertIn('instruction', route)

    def test_invalid_pid_is_rejected_before_accessibility_or_screen_queries(self):
        for value in [True, False, None, '12', 0, -1, 1.25, 2**31, 10**100]:
            with self.subTest(value=value):
                reply = self.exchange([call('agent_screen_status', {'pid': value})])[0]
                self.assertTrue(reply['result']['isError'])
                self.assertIn('pid', reply['result']['content'][0]['text'])

    def test_invalid_move_arguments_are_rejected_before_window_mutation(self):
        for arguments in [{}, {'pid': 1}, {'pid': -1, 'window_index': 0},
                          {'pid': 1, 'window_index': True}, {'pid': 1, 'window_index': -1},
                          {'pid': 1, 'window_index': 0.5}, {'pid': 1, 'window_index': 2**31},
                          {'pid': 1, 'window_index': 0, 'extra': 'ignored?'}]:
            with self.subTest(arguments=arguments):
                reply = self.exchange([call('agent_screen_move', arguments)])[0]
                self.assertTrue(reply['result']['isError'])

    def test_wrong_argument_shape_and_unknown_fields_are_rejected(self):
        for tool, arguments in [('agent_screen_prepare', {'pid': 1}),
                                ('agent_screen_status', {'pid_typo': 1}),
                                ('agent_screen_move', []), ('unknown', {})]:
            reply = self.exchange([call(tool, arguments)])[0]
            self.assertTrue(reply['result']['isError'])

    def test_notifications_and_idless_tool_calls_never_execute(self):
        notification = call('agent_screen_move', {'pid': 1, 'window_index': 0})
        del notification['id']
        replies = self.exchange([notification, {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                 {'jsonrpc': '2.0', 'id': 'after', 'method': 'ping'}])
        self.assertEqual(replies, [{'jsonrpc': '2.0', 'id': 'after', 'result': {}}])

    def test_malformed_requests_recover_for_next_request(self):
        replies = self.exchange(['{not-json}', '[]', {'jsonrpc': '2.0', 'id': True, 'method': 'ping'},
                                 {'jsonrpc': '2.0', 'id': 4, 'method': 'ping', 'params': []},
                                 {'jsonrpc': '2.0', 'id': 5, 'method': 'ping'}])
        self.assertEqual([reply['error']['code'] for reply in replies[:4]], [-32700, -32600, -32600, -32602])
        self.assertEqual(replies[-1]['result'], {})

    def test_oversized_frame_is_rejected_and_stream_recovers(self):
        replies = self.exchange(['x' * 100_000, {'jsonrpc': '2.0', 'id': 2, 'method': 'ping'}])
        self.assertEqual(replies[0]['error']['code'], -32700)
        self.assertIn('64 KiB', replies[0]['error']['message'])
        self.assertEqual(replies[1]['result'], {})

    def test_cli_invalid_pid_never_falls_back_to_all_apps(self):
        for value in ['oops', '0', '-1', str(2**31)]:
            result = subprocess.run([str(BINARY), 'status', value], text=True,
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')

    def test_public_files_exclude_machine_paths_and_credentials(self):
        forbidden = ['BEGIN ' + 'PRIVATE KEY']
        for path in ROOT.rglob('*'):
            if not path.is_file() or any(part in {'.build', '.git', '__pycache__'} for part in path.parts):
                continue
            text = path.read_text()
            self.assertIsNone(re.search(r'/Users/[A-Za-z0-9_-]+/', text), str(path.relative_to(ROOT)))
            self.assertIsNone(re.search(r'gh[pousr]_[A-Za-z0-9]{20,}', text), str(path.relative_to(ROOT)))
            for marker in forbidden:
                self.assertNotIn(marker, text, str(path.relative_to(ROOT)))


if __name__ == '__main__':
    unittest.main()
