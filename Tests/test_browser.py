import base64
import contextlib
import http.client
import http.cookies
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest import mock

import AppKit

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'skills' / 'agent-screen' / 'scripts' / 'agent-browser.py'
spec = importlib.util.spec_from_file_location('agent_browser_under_test', SCRIPT)
browser_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(browser_module)


def blank_png():
    representation = AppKit.NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, 96, 96, 8, 4, True, False, AppKit.NSDeviceRGBColorSpace, 0, 0)
    return bytes(representation.representationUsingType_properties_(
        AppKit.NSBitmapImageFileTypePNG, {}))


class FakeSocket:
    def __init__(self, incoming=b''):
        self.incoming = bytearray(incoming)
        self.outgoing = bytearray()

    def recv(self, count):
        result = bytes(self.incoming[:count])
        del self.incoming[:count]
        return result

    def sendall(self, data):
        self.outgoing.extend(data)

    def settimeout(self, timeout):
        self.timeout = timeout

    def close(self):
        pass


class FakeTransport:
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.sent = []
        self.closed = False

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def receive_text(self):
        if self.replies:
            return json.dumps(self.replies.pop(0))
        return json.dumps({'method': 'idle'})

    def close(self):
        self.closed = True


class FakeCDP:
    def __init__(self, image):
        self.image = image
        self.calls = []
        self.counter = 0

    def call(self, method, params=None, session_id=None, timeout=30):
        self.calls.append((method, params or {}, session_id))
        params = params or {}
        if method == 'Target.createBrowserContext':
            self.counter += 1
            return {'browserContextId': f'context-{self.counter}'}
        if method == 'Target.createTarget':
            return {'targetId': f'target-{self.counter}'}
        if method == 'Target.attachToTarget':
            return {'sessionId': f'cdp-{self.counter}'}
        if method == 'Target.getTargetInfo':
            return {'targetInfo': {'title': 'Fixture', 'url': 'https://example.com'}}
        if method == 'Target.getTargets':
            return {'targetInfos': []}
        if method == 'Page.captureScreenshot':
            return {'data': base64.b64encode(self.image).decode('ascii')}
        if method == 'Runtime.evaluate':
            expression = params['expression']
            if expression == 'document.readyState':
                value = 'complete'
            elif 'const visible = el' in expression:
                value = {'title': 'Fixture', 'url': 'https://example.com', 'text': 'fixture', 'elements': []}
            elif 'getBoundingClientRect' in expression:
                value = {'x': 40, 'y': 50}
            elif 'document.activeElement === el' in expression:
                value = True
            elif 'querySelector' in expression and 'el.type' in expression:
                value = '1'
            else:
                value = True
            return {'result': {'value': value}}
        return {}


class FakeBrowser:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)
        self.cdp = FakeCDP(blank_png())
        self.running = False
        self.created = []
        self.stopped = False

    def call(self, method, params=None, session_id=None, timeout=30):
        return self.cdp.call(method, params, session_id, timeout)

    def create(self, session_id, width, height, url):
        self.running = True
        self.created.append(session_id)
        return browser_module.BrowserSession(
            self, session_id, f'context-{session_id}', f'target-{session_id}', f'cdp-{session_id}', width, height)

    def destroy(self, session):
        session.closed = True

    def stop(self):
        self.running = False
        self.stopped = True

    def tabs(self, session):
        return [{'id': session.target_id, 'title': 'Fixture', 'url': 'https://example.com', 'active': True}]

    def new_tab(self, session, url):
        return session

    def attach(self, session, target):
        session.target_id = target


class FakeProcess:
    pid = 12345
    endpoint = 'ws://fake'

    def __init__(self):
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.terminated = True
        return 0


class TransportTests(unittest.TestCase):
    def test_client_frames_are_masked_and_fragmented_messages_accept_ping(self):
        sock = FakeSocket()
        ws = browser_module.WebSocket('ws://unused', connection=sock)
        ws.send_text('hello')
        data = bytes(sock.outgoing)
        self.assertEqual(data[0], 0x81)
        self.assertTrue(data[1] & 0x80)
        mask = data[2:6]
        self.assertEqual(bytes(value ^ mask[index % 4] for index, value in enumerate(data[6:])), b'hello')

        sock = FakeSocket(b'\x01\x03hel\x89\x01x\x80\x02lo')
        ws = browser_module.WebSocket('ws://unused', connection=sock)
        self.assertEqual(ws.receive_text(), 'hello')
        self.assertEqual(sock.outgoing[0], 0x8A)

    def test_fragmented_message_cannot_start_a_new_message_in_the_middle(self):
        sock = FakeSocket(b'\x01\x00\x81\x01x')
        ws = browser_module.WebSocket('ws://unused', connection=sock)
        with self.assertRaises(browser_module.BrowserError):
            ws.receive_text()

    def test_cdp_routes_replies_and_rejects_errors_and_timeout(self):
        transport = FakeTransport([{'id': 1, 'result': {'value': 2}}])
        cdp = browser_module.CDP(transport)
        self.assertEqual(cdp.call('Runtime.evaluate'), {'value': 2})
        self.assertEqual(transport.sent[0]['method'], 'Runtime.evaluate')
        failing = browser_module.CDP(FakeTransport([{'id': 1, 'error': {'message': 'bad'}}]))
        with self.assertRaises(browser_module.BrowserError):
            failing.call('Page.enable')
        timed = browser_module.CDP(FakeTransport())
        with self.assertRaises(browser_module.BrowserError) as caught:
            timed.call('Page.enable', timeout=0.001)
        self.assertEqual(caught.exception.code, 'cdp_timeout')

    def test_dialog_interrupts_runtime_evaluation_instead_of_waiting(self):
        cdp = browser_module.CDP(FakeTransport([
            {'method': 'Page.javascriptDialogOpening', 'params': {'type': 'alert'}}]))
        with self.assertRaises(browser_module.BrowserError) as caught:
            cdp.call('Runtime.evaluate')
        self.assertEqual(caught.exception.code, 'dialog_pending')


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.browser = FakeBrowser(self.temp.name)
        self.session = browser_module.BrowserSession(
            self.browser, 'alpha', 'context', 'target', 'cdp', 1280, 900)

    def tearDown(self):
        self.temp.cleanup()

    def test_input_uses_devtools_and_never_the_shared_clipboard(self):
        result = self.session.click(selector='#button')
        self.assertEqual(result['pointer'], {'x': 40.0, 'y': 50.0})
        methods = [call[0] for call in self.browser.cdp.calls]
        self.assertIn('Input.dispatchMouseEvent', methods)
        self.session.type('#field', '你好', replace=True)
        insert = next(call for call in self.browser.cdp.calls if call[0] == 'Input.insertText')
        self.assertEqual(insert[1]['text'], '你好')
        with self.assertRaises(browser_module.BrowserError):
            self.session.key('cmd+v')
        with self.assertRaises(browser_module.BrowserError):
            self.session.move(-1, 0)
        with self.assertRaises(browser_module.BrowserError):
            self.session.scroll(0, 0)

    def test_url_and_external_protocol_refusals(self):
        for value in ['file:///tmp/private', 'mailto:user@example.com', 'chrome://settings']:
            with self.subTest(value=value), self.assertRaises(browser_module.BrowserError):
                browser_module.navigation_url(value)
        self.assertEqual(browser_module.navigation_url('https://example.com/a'), 'https://example.com/a')
        with self.assertRaises(browser_module.BrowserError):
            browser_module.navigation_url('https://user:password@example.com')

    def test_cursor_is_actually_composited_into_png(self):
        raw = self.session.frame(raw=True)
        annotated = self.session.frame()
        self.assertNotEqual(raw, annotated)
        image = AppKit.NSImage.alloc().initWithData_(annotated)
        self.assertIsNotNone(image)
        self.assertGreater(image.size().width, 0)

    def test_dialog_snapshot_is_nonblocking(self):
        self.session.dialog = {'type': 'alert', 'message': 'fixture'}
        result = self.session.snapshot()
        self.assertEqual(result['dialog']['message'], 'fixture')
        self.assertEqual(result['elements'], [])
        self.session.handle_dialog(True)
        self.assertIsNone(self.session.dialog)

    def test_download_info_refreshes_and_finds_the_private_file(self):
        self.session.downloads['guid'] = {
            'guid': 'guid', 'suggestedFilename': 'fixture.txt',
            'url': 'data:text/plain,hello', 'state': 'completed'}
        (self.session.download_dir / 'fixture.txt').write_text('hello')
        result = self.session.download_info()
        self.assertEqual(result[0]['state'], 'completed')
        self.assertEqual(result[0]['bytes'], 5)
        self.assertEqual(Path(result[0]['path']).parent, self.session.download_dir)

    def test_browser_download_event_without_a_session_id_uses_frame_ownership(self):
        self.browser.session_map = {'cdp': self.session}
        self.browser.download_owners = {}
        browser_module.Chrome.event(self.browser, {'method': 'Browser.downloadWillBegin', 'params': {
            'frameId': 'target', 'guid': 'guid', 'suggestedFilename': 'fixture.txt', 'url': 'data:'}})
        browser_module.Chrome.event(self.browser, {'method': 'Browser.downloadProgress', 'params': {
            'guid': 'guid', 'state': 'completed'}})
        self.assertEqual(self.session.downloads['guid']['state'], 'completed')

    def test_upload_rejects_secrets_symlinks_and_bad_shapes(self):
        with self.assertRaises(browser_module.BrowserError):
            browser_module.validate_upload('/tmp/.env')
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / 'document.txt'
            file.write_text('fixture')
            link = Path(directory) / 'link.txt'
            link.symlink_to(file)
            self.assertEqual(browser_module.validate_upload(str(file)), str(file))
            with self.assertRaises(browser_module.BrowserError):
                browser_module.validate_upload(str(link))
        with self.assertRaises(browser_module.BrowserError):
            self.session.type('#field', 'x', replace='yes')
        with self.assertRaises(browser_module.BrowserError):
            self.session.frame(raw='yes')


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.browser = FakeBrowser(self.temp.name)
        self.daemon = browser_module.Daemon(
            state_dir=self.temp.name, browser=self.browser, viewer_html='<html>test viewer</html>')
        self.daemon.httpd = browser_module.make_viewer_server(self.daemon)
        self.viewer_thread = threading.Thread(target=self.daemon.httpd.serve_forever, daemon=True)
        self.viewer_thread.start()
        self.created = self.daemon.handle({'cmd': 'create', 'params': {
            'session': 'alpha', 'url': 'about:blank', 'width': 1280, 'height': 900}})
        self.lease = self.created['lease']

    def tearDown(self):
        self.daemon.httpd.shutdown()
        self.daemon.httpd.server_close()
        self.viewer_thread.join(timeout=2)
        self.browser.stop()
        self.temp.cleanup()

    def test_lease_is_private_and_required_for_session_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            browser_module.emit(self.created)
        self.assertNotIn(self.lease, output.getvalue())
        result = self.daemon.handle({'cmd': 'snapshot', 'params': {
            'session': 'alpha', 'lease': self.lease, 'max_chars': 100, 'limit': 1, 'selector': None}})
        self.assertEqual(result['route'], 'managed_headless')
        with self.assertRaises(browser_module.BrowserError) as caught:
            self.daemon.handle({'cmd': 'snapshot', 'params': {'session': 'alpha', 'lease': 'wrong'}})
        self.assertEqual(caught.exception.code, 'invalid_lease')
        self.assertIn('alpha', self.daemon.sessions)
        destroyed = self.daemon.handle({'cmd': 'destroy', 'params': {
            'session': 'alpha', 'lease': self.lease}})
        self.assertTrue(destroyed['destroyed'])
        self.assertFalse(self.browser.running)

    def test_viewer_ticket_cookie_origin_and_input(self):
        shown = self.daemon.handle({'cmd': 'show', 'params': {'session': 'alpha', 'lease': self.lease}})
        url = shown['url']
        connection = http.client.HTTPConnection('127.0.0.1', self.daemon.httpd.server_port, timeout=3)
        connection.request('GET', url)
        response = connection.getresponse()
        self.assertEqual(response.status, 303)
        cookie_header = response.getheader('Set-Cookie')
        self.assertIn('HttpOnly', cookie_header)
        self.assertIn('SameSite=Strict', cookie_header)
        cookie = http.cookies.SimpleCookie(cookie_header)['agent_browser_viewer'].value
        location = response.getheader('Location')
        response.read()

        connection.request('GET', location, headers={'Cookie': f'agent_browser_viewer={cookie}'})
        page = connection.getresponse()
        self.assertEqual(page.status, 200)
        html = page.read().decode()
        self.assertNotIn(self.daemon.viewer_token, html)
        self.assertNotIn(self.lease, html)

        connection.request('GET', '/api/state?session=alpha', headers={
            'Cookie': f'agent_browser_viewer={cookie}', 'Sec-Fetch-Site': 'same-origin'})
        state = connection.getresponse()
        self.assertEqual(state.status, 200)
        payload = json.loads(state.read())
        self.assertEqual(payload['title'], 'Fixture')
        self.assertIn('png', payload)
        self.assertNotIn(self.lease, json.dumps(payload))

        connection.request('GET', url)
        self.assertEqual(connection.getresponse().status, 403)
        connection.request('GET', '/api/state?session=alpha', headers={'Host': 'evil.example'})
        self.assertEqual(connection.getresponse().status, 403)

        body = json.dumps({'kind': 'click', 'x': 40, 'y': 50})
        connection.request('POST', '/api/input?session=alpha', body=body, headers={
            'Cookie': f'agent_browser_viewer={cookie}', 'Origin': f'http://127.0.0.1:{self.daemon.httpd.server_port}',
            'Sec-Fetch-Site': 'same-origin', 'Content-Type': 'application/json'})
        result = connection.getresponse()
        self.assertEqual(result.status, 200)
        result.read()
        self.assertEqual(self.daemon.sessions['alpha'].cursor, {'x': 40.0, 'y': 50.0})
        connection.close()

    def test_viewer_static_page_defaults_to_read_only(self):
        html = (ROOT / 'skills' / 'agent-screen' / 'scripts' / 'agent-browser-viewer.html').read_text()
        self.assertIn('id="control" type="checkbox"', html)
        self.assertIn('compose.disabled = !control.checked', html)
        self.assertNotIn('agent_browser_viewer=', html)
        self.assertNotIn('innerHTML', html)


class LifecycleTests(unittest.TestCase):
    def test_chrome_launch_is_headless_and_cleans_private_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            captured = []
            process = FakeProcess()
            transport = FakeTransport([{'id': 1, 'result': {}}])
            chrome = browser_module.Chrome(
                state_dir=directory, executable='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
                launcher=lambda args: captured.append(args) or process,
                connector=lambda endpoint: transport)
            chrome.start()
            arguments = captured[0]
            self.assertIn('--headless=new', arguments)
            self.assertIn('--use-mock-keychain', arguments)
            self.assertNotIn('--no-sandbox', arguments)
            self.assertNotIn('--remote-allow-origins=*', arguments)
            profile = Path(next(arg.split('=', 1)[1] for arg in arguments if arg.startswith('--user-data-dir=')))
            self.assertEqual(profile.parent, Path(directory) / 'profiles')
            self.assertTrue(chrome.state_path.exists())
            chrome.stop()
            self.assertTrue(process.terminated)
            self.assertFalse(profile.exists())
            self.assertFalse(chrome.state_path.exists())

    def test_orphan_recovery_never_kills_a_different_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / 'profiles' / 'profile-test'
            profile.mkdir(parents=True)
            browser_module.atomic_json(root / 'browser.json', {
                'profile': str(profile), 'pid': os.getpid(),
                'executable': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'})
            browser_module.Chrome.recover_orphan(root)
            self.assertFalse(profile.exists())
            self.assertFalse((root / 'browser.json').exists())
        self.assertFalse(browser_module.Chrome.owns_command(
            '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome --restore-last-session',
            '/tmp/agent-browser-profile', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'))

    def test_socket_probe_does_not_delete_a_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'daemon.sock'
            path.write_text('not a socket')
            daemon = browser_module.Daemon(state_dir=directory, browser=FakeBrowser(directory),
                                           viewer_html='<html></html>')
            with mock.patch.object(browser_module, 'SOCKET_PATH', path):
                with self.assertRaises(browser_module.BrowserError):
                    daemon.serve()
            self.assertTrue(path.exists())
            daemon.httpd.shutdown()
            daemon.httpd.server_close()
            daemon.lock_handle.close()

    def test_client_create_never_prints_lease(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(browser_module, 'STATE_DIR', Path(directory)), \
                mock.patch.object(browser_module, 'remote', return_value={
                    'session': 'alpha', 'lease': 'test-lease', 'route': 'managed_headless'}):
            args = type('Args', (), {'session': 'alpha', 'url': 'about:blank', 'width': 1280, 'height': 900})()
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                browser_module.create_session(args)
            self.assertNotIn('test-lease', output.getvalue())
            path = Path(directory) / 'client' / 'alpha.json'
            self.assertIn('test-lease', path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
