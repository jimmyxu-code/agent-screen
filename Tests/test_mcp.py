import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / '.build' / 'agent-screen'
# The suite must never move a real window: serve tools without hosting the claim engine.
ENV = {**os.environ, 'AGENT_SCREEN_ENGINE': 'off'}


def call(tool, arguments, request_id=1):
    return {'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call',
            'params': {'name': tool, 'arguments': arguments}}


class ProtocolTests(unittest.TestCase):
    def exchange(self, lines):
        payload = '\n'.join(line if isinstance(line, str) else json.dumps(line) for line in lines) + '\n'
        result = subprocess.run([str(BINARY), 'mcp'], input=payload, text=True, env=ENV,
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
        self.assertEqual(replies[0]['result']['serverInfo']['version'], '1.7.0')
        catalogue = replies[1]['result']['tools']
        self.assertEqual({tool['name'] for tool in catalogue},
                         {'agent_screen_prepare', 'agent_screen_status', 'agent_screen_park',
                          'agent_screen_claim', 'agent_screen_release', 'agent_screen_restore',
                          'agent_screen_move', 'agent_screen_cursor'})
        self.assertTrue(all(tool['inputSchema']['additionalProperties'] is False for tool in catalogue))
        self.assertEqual(replies[2]['result'], {})

    def test_catalogue_has_current_single_screen_route(self):
        catalogue = self.exchange([{'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}])[0]['result']['tools']
        descriptions = {tool['name']: tool['description'] for tool in catalogue}
        for name in ['agent_screen_prepare', 'agent_screen_status']:
            self.assertIn('physical screen', descriptions[name])
            self.assertNotIn('operate directly', descriptions[name])
        self.assertIn('SpaceO', descriptions['agent_screen_prepare'])
        for name in ['agent_screen_park', 'agent_screen_move', 'agent_screen_claim']:
            self.assertIn('controller', descriptions[name])
        # claim is the preferred route for windows the task opens; park is for pre-existing ones.
        self.assertIn('agent_screen_claim', descriptions['agent_screen_prepare'])
        self.assertIn('Preferred', descriptions['agent_screen_claim'])
        self.assertIn('claim the app instead', descriptions['agent_screen_park'])

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
        # The agent screen is the built-in panel by hardware, not simply the second entry:
        # display ordering changes with hot-plug and with whichever screen is main.
        target = next((s for s in screens if s.get('built_in')), screens[1 if multiple else 0])
        self.assertEqual(route['mode'], 'second_physical_screen' if multiple else 'single_physical_screen')
        self.assertEqual(route['target_display_id'], target['id'])
        self.assertEqual(route['target_screen'], target['name'])
        self.assertTrue(route['placement_required'])
        self.assertEqual(route['virtual_screen_required'], not multiple)
        self.assertEqual(route['target_kind'], 'built_in' if multiple else 'spaceo_virtual')
        self.assertEqual(route['browser_route'], 'managed_headless')
        self.assertEqual(route['browser_command'], 'agent-browser')
        self.assertIn('agent-browser show', route['browser_viewer'])
        # Whether a claim will actually be enforced must be observable, not assumed.
        self.assertIn('engine_running', route)
        self.assertIsInstance(route['engine_running'], bool)
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
            result = subprocess.run([str(BINARY), 'status', value], text=True, env=ENV,
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, '')

    def test_cli_rejects_malformed_arguments_instead_of_guessing(self):
        # A typo must never fall back to window 0, a default lease, or every window of the app.
        for args in [['park', '1', 'abc'], ['park', '1', '0', 'soon'], ['park', '1', '-1'],
                     ['park', '1', '0', '0.5'], ['restore', '1', 'abc'], ['restore', '--all', '1'],
                     ['claim', '1', 'later'], ['claim', '1', '1e300'], ['park', '1', '0', '99999'], ['claim'], ['release'], ['release', '1', '2'],
                     ['sweep', 'x'], ['prepare', 'extra'], ['watch', 'extra'], ['mcp', 'extra'],
                     ['unknown']]:
            with self.subTest(args=args):
                result = subprocess.run([str(BINARY), *args], text=True, env=ENV,
                                        capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')

    def test_invalid_claim_and_lease_arguments_are_rejected(self):
        for tool, arguments in [('agent_screen_claim', {}), ('agent_screen_claim', {'pid': 0}),
                                ('agent_screen_claim', {'pid': 1, 'timeout_minutes': 0.5}),
                                ('agent_screen_claim', {'pid': 1, 'timeout_minutes': True}),
                                ('agent_screen_claim', {'pid': 1, 'timeout_minutes': 1e300}),
                                ('agent_screen_park', {'pid': 1, 'timeout_minutes': 1441}),
                                ('agent_screen_claim', {'pid': 1, 'window_index': 0}),
                                ('agent_screen_park', {'pid': 1, 'timeout_minutes': 'ten'}),
                                ('agent_screen_release', {}), ('agent_screen_release', {'all': 'yes'}),
                                ('agent_screen_restore', {}), ('agent_screen_restore', {'pid': 1, 'all': 1})]:
            with self.subTest(tool=tool, arguments=arguments):
                reply = self.exchange([call(tool, arguments)])[0]
                self.assertTrue(reply['result']['isError'])

    def test_cursor_is_validated_and_disabled_during_tests(self):
        invalid = [{}, {'action': 'unknown'}, {'action': 'status', 'x': 1},
                   {'action': 'move', 'x': 1}, {'action': 'move', 'x': True, 'y': 2},
                   {'action': 'move', 'x': '1', 'y': 2}, {'action': 'move', 'x': 1e300, 'y': 2},
                   {'action': 'click', 'x': 1, 'y': 2},
                   {'action': 'click', 'x': 1, 'y': 2, 'pid': 0},
                   {'action': 'click', 'x': 1, 'y': 2, 'pid': True},
                   {'action': 'click', 'x': 1, 'y': 2, 'pid': 1, 'double': 1},
                   {'action': 'click', 'x': 1, 'y': 2, 'pid': 1, 'shift': 'yes'},
                   {'action': 'click', 'x': 1, 'y': 2, 'pid': 1, 'extra': True}]
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                reply = self.exchange([call('agent_screen_cursor', arguments)])[0]
                self.assertTrue(reply['result']['isError'])
        for arguments in [{'action': 'status'}, {'action': 'move', 'x': 2000, 'y': 100},
                          {'action': 'click', 'x': 2000, 'y': 100, 'pid': 1}]:
            with self.subTest(test_mode=arguments):
                reply = self.exchange([call('agent_screen_cursor', arguments)])[0]
                self.assertTrue(reply['result']['isError'])
                self.assertIn('测试模式', reply['result']['content'][0]['text'])

    def test_cli_cursor_rejects_bad_input_before_backend(self):
        for args in [['cursor'], ['cursor', 'unknown'], ['cursor', 'status', 'extra'],
                     ['cursor', 'move', '1'], ['cursor', 'move', 'bad', '2'],
                     ['cursor', 'click', '1', '2', '--no-hide'],
                     ['cursor', 'click', '1', '2', '--pid', 'bad'],
                     ['cursor', 'click', '1', '2', '--pid', '1', '--pid', '2'],
                     ['cursor', 'click', '1', '2', '--pid', '1', '--double', '--double'],
                     ['cursor', 'click', '1', '2', '--double', '--double'], ['cursor', 'status']]:
            with self.subTest(args=args):
                result = subprocess.run([str(BINARY), *args], text=True, env=ENV,
                                        capture_output=True, timeout=10)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, '')

    def test_shared_skill_is_self_contained_and_not_client_specific(self):
        skill = (ROOT / 'skills' / 'agent-screen' / 'SKILL.md').read_text()
        self.assertTrue(skill.startswith('---\nname: agent-screen\ndescription: '))
        for required in ['agent-screen prepare', 'agent-screen watch', 'agent-screen cursor status',
                         'agent-input click X Y --pid PID', 'agent-spaceo create', 'agent-spaceo daemon',
                         'agent-spaceo heartbeat', 'agent-spaceo destroy', 'spaceo_virtual',
                         'agent-browser daemon', 'agent-browser create', 'agent-browser show',
                         'managed_headless', 'single_physical_screen', 'second_physical_screen',
                         'Never fall back to pointer tools']:
            self.assertIn(required, skill)
        for client in ['WorkBuddy', 'Codex', 'DeepSeek', 'Kimi', 'Claude']:
            self.assertNotIn(client, skill)
        makefile = (ROOT / 'Makefile').read_text()
        self.assertIn('$(HOME)/.agents/skills/agent-screen', makefile)
        self.assertIn('$(PREFIX)/bin/agent-input', makefile)
        self.assertIn('$(PREFIX)/bin/agent-spaceo', makefile)
        self.assertIn('$(PREFIX)/bin/agent-browser', makefile)
        self.assertIn('VIEWER_HOME = $(PREFIX)/share/agent-browser', makefile)
        self.assertIn('$(VIEWER_HOME)/viewer.html', makefile)

    def test_shared_spaceo_defaults_reuse_one_virtual_display(self):
        import importlib.util
        from unittest import mock

        spec = importlib.util.spec_from_file_location(
            'agent_spaceo_defaults', ROOT / 'skills' / 'agent-screen' / 'scripts' / 'agent-spaceo.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.object(module.os, 'execv') as execv:
            module.daemon(module.parser().parse_args(['daemon']))
        execv.assert_called_once_with(
            module.SPACEO_BIN, [module.SPACEO_BIN, 'daemon', '--sessions-per-display', '4'])
        create = module.parser().parse_args(['create', '--session', 'alpha'])
        self.assertEqual(create.preset, 'shared')

        for report, message in [
            ({'displays': [{'capacity': 4, 'used': 4}]}, '区域已满'),
            ({'displays': [{'capacity': 4, 'used': 1}, {'capacity': 4, 'used': 1}]}, '不再新增显示'),
        ]:
            with self.subTest(report=report):
                result = subprocess.CompletedProcess([], 0, stdout=json.dumps(report))
                with mock.patch.object(module, 'invoke', return_value=result):
                    with self.assertRaisesRegex(SystemExit, message):
                        module.require_shared_pool()

    def test_spaceo_creation_preserves_a_second_physical_screen(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest import mock

        spec = importlib.util.spec_from_file_location(
            'agent_spaceo_route_guard', ROOT / 'skills' / 'agent-screen' / 'scripts' / 'agent-spaceo.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        physical = {'mode': 'second_physical_screen', 'target_kind': 'built_in',
                    'target_built_in': True, 'virtual_screen_required': False}
        result = subprocess.CompletedProcess([], 0, stdout=json.dumps(physical))
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(module, 'STATE_DIR', Path(directory)), \
                mock.patch.object(module.subprocess, 'run', return_value=result) as prepare, \
                mock.patch.object(module, 'invoke') as backend:
            for preset in ['shared', 'exclusive']:
                with self.subTest(preset=preset), self.assertRaisesRegex(SystemExit, '本机屏'):
                    module.create(SimpleNamespace(session='alpha', preset=preset, controller_ttl=3600))
        self.assertEqual(prepare.call_count, 2)
        prepare.assert_called_with([module.AGENT_SCREEN_BIN, 'prepare'], text=True,
                                   capture_output=True, timeout=15)
        backend.assert_not_called()

        for result, message in [
            (subprocess.CompletedProcess([], 0, stdout='{}'), '路由不完整'),
            (subprocess.CompletedProcess([], 1, stdout=''), 'prepare 失败'),
            (subprocess.CompletedProcess([], 0, stdout='not-json'), '无效路由'),
        ]:
            with self.subTest(message=message), \
                    mock.patch.object(module.subprocess, 'run', return_value=result), \
                    mock.patch.object(module, 'invoke') as backend, \
                    self.assertRaisesRegex(SystemExit, message):
                module.create(SimpleNamespace(session='alpha', preset='shared', controller_ttl=3600))
            backend.assert_not_called()

    def test_spaceo_requires_the_companion_cursor_capability(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest import mock

        spec = importlib.util.spec_from_file_location(
            'agent_spaceo_capability', ROOT / 'skills' / 'agent-screen' / 'scripts' / 'agent-spaceo.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        cursor = {'name': 'session-cursors', 'available': True}
        for daemon, capabilities, message in [
            ({'running': False, 'matchesCLI': True}, [cursor], '后台未运行'),
            ({'running': True, 'matchesCLI': False}, [cursor], '构建不一致'),
            ({'running': True, 'matchesCLI': True}, [], 'session-cursors'),
            ({'running': True, 'matchesCLI': True},
             [{'name': 'session-cursors', 'available': False}], 'session-cursors'),
        ]:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                report = {'daemon': daemon, 'capabilities': capabilities}
                result = subprocess.CompletedProcess([], 0, stdout=json.dumps(report))
                with mock.patch.object(module, 'STATE_DIR', Path(directory)), \
                        mock.patch.object(module, 'require_single_physical_screen'), \
                        mock.patch.object(module, 'invoke', return_value=result) as backend, \
                        self.assertRaisesRegex(SystemExit, message):
                    module.create(SimpleNamespace(session='alpha', preset='shared', controller_ttl=3600))
                backend.assert_called_once_with(['doctor', '--json'])
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_spaceo_destroy_prunes_only_definitively_missing_state(self):
        import importlib.util
        from types import SimpleNamespace
        from unittest import mock

        spec = importlib.util.spec_from_file_location(
            'agent_spaceo_destroy', ROOT / 'skills' / 'agent-screen' / 'scripts' / 'agent-spaceo.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        failed = subprocess.CompletedProcess([], 1, stdout='')
        for listing, should_remove in [
            (subprocess.CompletedProcess([], 0, stdout='{"sessions":[]}'), True),
            (subprocess.CompletedProcess([], 0, stdout='{"sessions":[{"id":"alpha"}]}'), False),
            (subprocess.CompletedProcess([], 1, stdout=''), False),
        ]:
            with self.subTest(listing=listing, should_remove=should_remove):
                with mock.patch.object(module, 'load_state', return_value={'lease': 'test-lease'}), \
                        mock.patch.object(module, 'invoke', side_effect=[failed, listing]), \
                        mock.patch.object(module, 'remove_state') as remove:
                    module.destroy(SimpleNamespace(session='alpha', keep_apps=False))
                if should_remove:
                    remove.assert_called_once_with('alpha')
                else:
                    remove.assert_not_called()

    def test_spaceo_cli_keeps_leases_private_and_preserves_exit_codes(self):
        script = ROOT / 'skills' / 'agent-screen' / 'scripts' / 'agent-spaceo.py'
        compile(script.read_text(), str(script), 'exec')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'spaceo'
            fake.write_text('''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
if "prepare" in args:
    print(json.dumps({"mode": "single_physical_screen", "target_kind": "spaceo_virtual",
                      "virtual_screen_required": True}))
elif "doctor" in args:
    print(json.dumps({"ok": True, "daemon": {"running": True, "matchesCLI": True},
                      "capabilities": [{"name": "session-cursors", "available": True}]}))
elif "create" in args:
    session = args[args.index("--session") + 1]
    print(json.dumps({"session": {"id": session, "displayID": 77, "x": 100, "y": 200,
                                  "width": 1200, "height": 800},
                      "controllerLeaseID": "secret-lease"}))
elif "pool" in args:
    print(json.dumps({"displays": []}))
elif "heartbeat" in args:
    print(json.dumps({"controllerLeaseID": "new-secret-lease"}))
else:
    print(json.dumps({"ok": True}))
''')
            fake.chmod(0o755)
            env = {**ENV, 'SPACEO_BIN': str(fake), 'AGENT_SCREEN_BIN': str(fake),
                   'AGENT_SPACEO_STATE_DIR': str(root / 'state')}

            def call(*args, check=True):
                return subprocess.run(['python3', str(script), *args], text=True,
                                      capture_output=True, env=env, check=check)

            doctor = json.loads(call('doctor').stdout)
            self.assertTrue(doctor['daemon']['matchesCLI'])
            self.assertEqual(doctor['capabilities'],
                             [{'name': 'session-cursors', 'available': True}])

            created = call('create', '--session', 'alpha')
            self.assertNotIn('secret-lease', created.stdout)
            payload = json.loads(created.stdout)
            self.assertEqual(payload['display_id'], 77)
            self.assertEqual(payload['tile'], {'x': 100, 'y': 200, 'width': 1200, 'height': 800})
            state = root / 'state' / 'alpha.json'
            self.assertIn('secret-lease', state.read_text())
            self.assertEqual(state.stat().st_mode & 0o777, 0o600)

            launched = call('run', '--session', 'alpha', '--app', 'Fixture')
            self.assertNotIn('secret-lease', launched.stdout)
            renewed = call('heartbeat', '--session', 'alpha')
            self.assertNotIn('new-secret-lease', renewed.stdout)
            self.assertIn('new-secret-lease', state.read_text())
            self.assertNotIn('new-secret-lease', call('command', '--session', 'alpha', 'ax').stdout)
            call('destroy', '--session', 'alpha')
            self.assertFalse(state.exists())

            invalid = call('create', '--session', 'bad/name', check=False)
            self.assertNotEqual(invalid.returncode, 0)
            invalid_ttl = call('create', '--session', 'beta', '--controller-ttl', '10', check=False)
            self.assertNotEqual(invalid_ttl.returncode, 0)

    def test_input_never_borrows_the_real_pointer(self):
        path = ROOT / 'skills' / 'agent-screen' / 'scripts' / 'agent-input.py'
        source = path.read_text()
        compile(source, str(path), 'exec')
        for forbidden in ['CGWarpMouseCursorPosition', 'CGDisplayHideCursor', 'CGDisplayShowCursor',
                          'CGAssociateMouseAndMouseCursorPosition', 'def _post_mouse', '--no-hide']:
            self.assertNotIn(forbidden, source)
        self.assertIn('return cursor("click", x, y', source)
        self.assertNotIn('require_front(pid)\n    return cursor("click"', source)
        swift = (ROOT / 'agent-screen.swift').read_text()
        self.assertIn('setCursorWindowLocation(event, localPoint)', swift)
        self.assertIn('event.postToPid(target.pid)', swift)
        for forbidden in ['CGWarpMouseCursorPosition', 'CGDisplayHideCursor', 'CGDisplayShowCursor',
                          'CGAssociateMouseAndMouseCursorPosition', '.post(tap:']:
            self.assertNotIn(forbidden, swift)

    def test_browser_never_uses_user_browser_or_real_pointer(self):
        path = ROOT / 'skills' / 'agent-screen' / 'scripts' / 'agent-browser.py'
        source = path.read_text()
        compile(source, str(path), 'exec')
        for required in ['--headless=new', '--remote-debugging-address=127.0.0.1',
                         'Target.createBrowserContext', 'Input.insertText',
                         'HttpOnly; SameSite=Strict']:
            self.assertIn(required, source)
        for forbidden in ['CGWarpMouseCursorPosition', 'CGDisplayHideCursor', 'CGDisplayShowCursor',
                          'CGAssociateMouseAndMouseCursorPosition', 'CGEventPostToPid',
                          'SLPSGetKeyFocusProcess', 'SLPSGetTypingFocusProcess',
                          'SLPSSetFrontProcessWithOptions', 'openApplication', '.activate(',
                          'NSApplication.shared', '--no-sandbox', '--remote-allow-origins=*']:
            self.assertNotIn(forbidden, source)

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
