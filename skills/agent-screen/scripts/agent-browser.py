#!/usr/bin/env python3
"""Managed headless Chromium sessions with a private local live viewer."""

import argparse
import base64
import collections
import contextlib
import fcntl
import hashlib
import hmac
import http.cookies
import http.server
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse

import AppKit

VERSION = "1.0"
STATE_DIR = Path(os.environ.get("AGENT_BROWSER_STATE_DIR", os.path.expanduser("~/.local/state/agent-browser")))
SOCKET_PATH = Path(os.environ.get("AGENT_BROWSER_SOCKET", str(STATE_DIR / "daemon.sock")))
CHROME_PATH = os.environ.get(
    "AGENT_BROWSER_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
VIEWER_HTML = Path(os.environ.get(
    "AGENT_BROWSER_VIEWER_HTML",
    str(Path(__file__).with_name("agent-browser-viewer.html")
        if Path(__file__).with_name("agent-browser-viewer.html").is_file()
        else Path.home() / ".local/share/agent-browser/viewer.html")))
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_REQUEST = 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
MAX_IMAGE = 32 * 1024 * 1024
SENSITIVE_FILES = {
    ".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "secrets",
    "secrets.json", "credentials.json", ".netrc", "login data", "cookies",
}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".kdbx"}
LEASE_KEYS = {"lease", "ticket", "cookie", "websocketdebuggerurl"}


class BrowserError(RuntimeError):
    def __init__(self, message, code="browser_error"):
        super().__init__(message)
        self.code = code


def require(condition, message, code="bad_request"):
    if not condition:
        raise BrowserError(message, code)


def valid_session(value):
    require(isinstance(value, str) and SESSION_ID.fullmatch(value),
            "session 名称须为 1–64 位字母、数字、点、下划线或短横线")
    return value


def private_directory(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def atomic_json(path, value):
    private_directory(path.parent)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def bounded_text(value, name, maximum=4096, allow_empty=False):
    require(isinstance(value, str), f"{name} 必须是字符串")
    require(bool(value) or allow_empty, f"{name} 不能为空")
    require(len(value) <= maximum and not any(ord(c) < 32 and c not in "\n\t" or ord(c) == 127 for c in value),
            f"{name} 太长或含控制字符")
    return value


def navigation_url(value):
    value = bounded_text(value, "url")
    if value == "about:blank":
        return value
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
    except ValueError:
        raise BrowserError("url 无效")
    require(parsed.scheme.lower() in {"http", "https"} and parsed.netloc and hostname,
            "只允许 HTTP(S) 页面；外部协议、文件和浏览器内部页面不会打开")
    require(parsed.username is None and parsed.password is None, "url 不得包含账号或密码")
    return value


def coordinate(value, maximum, name):
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= maximum,
            f"{name} 必须在 0–{maximum} 之间")
    return float(value)


def dimensions(width, height):
    require(isinstance(width, int) and not isinstance(width, bool) and 320 <= width <= 4096,
            "宽度须为 320–4096")
    require(isinstance(height, int) and not isinstance(height, bool) and 240 <= height <= 2160,
            "高度须为 240–2160")
    return width, height


def send_json(connection, value):
    payload = (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    require(len(payload) <= MAX_RESPONSE, "响应超过大小限制", "response_too_large")
    connection.sendall(payload)


def receive_json(connection, maximum=MAX_RESPONSE):
    data = bytearray()
    while b"\n" not in data:
        chunk = connection.recv(min(65536, maximum + 1 - len(data)))
        require(bool(chunk), "后台连接已关闭", "connection_closed")
        data.extend(chunk)
        require(len(data) <= maximum, "消息超过大小限制", "message_too_large")
    line, _, remainder = bytes(data).partition(b"\n")
    require(not remainder, "后台发送了多余消息", "invalid_response")
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BrowserError("后台返回了无效 JSON", "invalid_response")
    require(isinstance(value, dict), "后台返回了无效响应", "invalid_response")
    return value


def remote(command, parameters=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(45)
        connection.connect(str(SOCKET_PATH))
        send_json(connection, {"cmd": command, "params": parameters or {}})
        response = receive_json(connection)
    if not response.get("ok"):
        raise BrowserError(response.get("error", "浏览器后台拒绝了操作"),
                           response.get("errorCode", "browser_error"))
    return response.get("result")


def sanitized(value):
    if isinstance(value, dict):
        return {key: sanitized(item) for key, item in value.items()
                if key.lower().replace("_", "") not in LEASE_KEYS}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


def emit(value):
    print(json.dumps(sanitized(value), ensure_ascii=False, allow_nan=False, sort_keys=True))


def client_path(session):
    valid_session(session)
    return STATE_DIR / "client" / f"{session}.json"


def client_state(session):
    path = client_path(session)
    require(path.is_file() and not path.is_symlink(), f"没有 session {session} 的本地 lease；请先 create",
            "missing_lease")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BrowserError(f"session 本地状态无法读取：{error}", "invalid_lease")
    require(state.get("version") == 1 and state.get("session") == session
            and isinstance(state.get("lease"), str) and state["lease"],
            "session 本地 lease 无效", "invalid_lease")
    return state


def save_client(session, lease):
    require(isinstance(lease, str) and lease, "后台未返回有效 lease", "invalid_response")
    path = client_path(session)
    require(not path.exists(), f"session {session} 已存在；请换名称或先 destroy", "session_exists")
    atomic_json(path, {"version": 1, "session": session, "lease": lease})
    os.chmod(path, 0o600)


def remove_client(session):
    with contextlib.suppress(FileNotFoundError):
        client_path(session).unlink()


def leased(session, command, parameters=None):
    state = client_state(session)
    return remote(command, {**(parameters or {}), "session": session, "lease": state["lease"]})


class WebSocket:
    def __init__(self, url, timeout=20, connection=None):
        self.socket = connection
        if self.socket is None:
            parts = urllib.parse.urlsplit(url)
            require(parts.scheme == "ws" and parts.hostname == "127.0.0.1"
                    and parts.port and parts.path.startswith("/devtools/")
                    and not parts.query and not parts.fragment,
                    "DevTools 地址不是受支持的本机连接", "invalid_devtools")
            self.socket = socket.create_connection((parts.hostname, parts.port), timeout=timeout)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {parts.path} HTTP/1.1\r\nHost: 127.0.0.1:{parts.port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
            self.socket.sendall(request.encode("ascii"))
            response = bytearray()
            while b"\r\n\r\n" not in response:
                chunk = self.socket.recv(2048)
                require(bool(chunk), "DevTools 握手失败", "devtools_handshake")
                response.extend(chunk)
                require(len(response) <= 16384, "DevTools 握手响应过大", "devtools_handshake")
            header, _, extra = bytes(response).partition(b"\r\n\r\n")
            require(not extra, "DevTools 握手包含多余数据", "devtools_handshake")
            lines = header.decode("iso-8859-1").split("\r\n")
            require(lines[0].startswith("HTTP/1.1 101"), "DevTools 握手被拒绝", "devtools_handshake")
            expected = base64.b64encode(hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            accept = next((line.split(":", 1)[1].strip() for line in lines[1:]
                           if line.lower().startswith("sec-websocket-accept:")), "")
            require(hmac.compare_digest(accept, expected), "DevTools 握手校验失败", "devtools_handshake")
        self.socket.settimeout(timeout)

    def read_exact(self, length):
        data = bytearray()
        while len(data) < length:
            chunk = self.socket.recv(length - len(data))
            require(bool(chunk), "DevTools 连接已关闭", "connection_closed")
            data.extend(chunk)
        return bytes(data)

    def send_frame(self, opcode, payload):
        require(len(payload) <= MAX_RESPONSE, "CDP 消息过大", "message_too_large")
        mask = os.urandom(4)
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length <= 65535:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.socket.sendall(bytes(header) + masked)

    def send_text(self, text):
        self.send_frame(1, text.encode("utf-8"))

    def receive_text(self):
        fragments = bytearray()
        fragmented = False
        while True:
            first, second = self.read_exact(2)
            require(first & 0x70 == 0 and second & 0x80 == 0, "DevTools 帧无效", "invalid_frame")
            opcode = first & 0x0f
            final = bool(first & 0x80)
            length = second & 0x7f
            if length == 126:
                length = struct.unpack("!H", self.read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self.read_exact(8))[0]
            require(length <= MAX_RESPONSE and len(fragments) + length <= MAX_RESPONSE,
                    "CDP 消息超过大小限制", "message_too_large")
            payload = self.read_exact(length)
            if opcode == 8:
                raise BrowserError("DevTools 已关闭连接", "connection_closed")
            if opcode == 9:
                require(final and length <= 125, "DevTools 控制帧无效", "invalid_frame")
                self.send_frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode == 1:
                require(not fragmented, "DevTools 分片状态无效", "invalid_frame")
                fragments.clear()
            elif opcode == 0:
                require(fragmented, "DevTools 分片状态无效", "invalid_frame")
            else:
                raise BrowserError("DevTools 仅支持文本消息", "invalid_frame")
            fragments.extend(payload)
            fragmented = not final
            if final:
                try:
                    return fragments.decode("utf-8")
                except UnicodeDecodeError:
                    raise BrowserError("DevTools 返回了无效文本", "invalid_frame")

    def close(self):
        with contextlib.suppress(Exception):
            self.send_frame(8, struct.pack("!H", 1000))
        with contextlib.suppress(Exception):
            self.socket.close()


class CDP:
    def __init__(self, transport, on_event=None):
        self.transport = transport
        self.on_event = on_event
        self.lock = threading.RLock()
        self.next_id = 0
        self.events = collections.deque(maxlen=256)

    def call(self, method, params=None, session_id=None, timeout=30):
        with self.lock:
            self.next_id += 1
            identifier = self.next_id
            message = {"id": identifier, "method": method, "params": params or {}}
            if session_id:
                message["sessionId"] = session_id
            self.transport.send_text(json.dumps(message, ensure_ascii=False, allow_nan=False))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                incoming = json.loads(self.transport.receive_text())
                if "method" in incoming:
                    self.events.append(incoming)
                    if self.on_event:
                        self.on_event(incoming)
                    if incoming["method"] == "Page.javascriptDialogOpening" and method == "Runtime.evaluate":
                        raise BrowserError("页面有 JavaScript 对话框等待处理", "dialog_pending")
                    continue
                if incoming.get("id") != identifier:
                    continue
                if "error" in incoming:
                    error = incoming["error"]
                    raise BrowserError(f"{method}: {error.get('message', 'CDP 拒绝了操作')}",
                                       "cdp_error")
                return incoming.get("result", {})
            raise BrowserError(f"{method}: 等待浏览器响应超时", "cdp_timeout")

    def notify(self, method, params, session_id=None):
        with self.lock:
            self.next_id += 1
            message = {"id": self.next_id, "method": method, "params": params}
            if session_id:
                message["sessionId"] = session_id
            self.transport.send_text(json.dumps(message, ensure_ascii=False, allow_nan=False))

    def close(self):
        self.transport.close()


class Chrome:
    def __init__(self, state_dir=STATE_DIR, executable=CHROME_PATH, launcher=None, connector=None):
        self.state_dir = Path(state_dir)
        self.executable = executable
        self.launcher = launcher or self.spawn
        self.connector = connector or WebSocket
        self.process = None
        self.profile = None
        self.cdp = None
        self.session_map = {}
        self.download_owners = {}
        self.lock = threading.RLock()

    @property
    def state_path(self):
        return self.state_dir / "browser.json"

    @property
    def profile_root(self):
        return self.state_dir / "profiles"

    @property
    def running(self):
        return self.process is not None and self.process.poll() is None and self.cdp is not None

    def spawn(self, arguments):
        return subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, start_new_session=True)

    def wait_endpoint(self):
        if hasattr(self.process, "endpoint"):
            return self.process.endpoint
        messages = queue.Queue()

        def pump():
            data = ""
            try:
                while True:
                    chunk = self.process.stderr.read1(4096)
                    if not chunk:
                        return
                    data = (data + chunk.decode("utf-8", "replace"))[-65536:]
                    match = re.search(r"DevTools listening on (ws://\S+)", data)
                    if match:
                        messages.put(match.group(1))
                        return
            finally:
                messages.put(None)

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                endpoint = messages.get(timeout=0.2)
            except queue.Empty:
                if self.process.poll() is not None:
                    break
                continue
            if endpoint:
                return endpoint
            if self.process.poll() is not None:
                break
        raise BrowserError("独立浏览器没有发布本机 DevTools 连接", "browser_start_failed")

    def start(self):
        with self.lock:
            if self.running:
                return
            require(Path(self.executable).is_file() and os.access(self.executable, os.X_OK),
                    "找不到可执行的 Chromium；请安装 Google Chrome 或设置 AGENT_BROWSER_CHROME",
                    "browser_missing")
            private_directory(self.profile_root)
            self.profile = Path(tempfile.mkdtemp(prefix="profile-", dir=self.profile_root))
            os.chmod(self.profile, 0o700)
            self.write_state(None)
            arguments = [
                self.executable, "--headless=new", "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble",
                "--disable-component-update", "--use-mock-keychain", "--mute-audio",
                "--window-size=1280,900", "about:blank",
            ]
            try:
                self.process = self.launcher(arguments)
                self.write_state(self.process.pid)
                endpoint = self.wait_endpoint()
                self.cdp = CDP(self.connector(endpoint), on_event=self.event)
            except Exception:
                self.stop()
                raise

    def write_state(self, pid):
        atomic_json(self.state_path, {"version": 1, "profile": str(self.profile),
                                     "pid": pid, "executable": self.executable})

    def call(self, method, params=None, session_id=None, timeout=30):
        self.start()
        require(self.cdp is not None, "独立浏览器不可用", "browser_unavailable")
        return self.cdp.call(method, params, session_id, timeout)

    def create(self, session_id, width, height, url):
        dimensions(width, height)
        navigation_url(url)
        with self.lock:
            context = self.call("Target.createBrowserContext")["browserContextId"]
            try:
                target = self.call("Target.createTarget", {
                    "url": "about:blank", "background": True, "browserContextId": context})["targetId"]
                attached = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})
                session = BrowserSession(self, session_id, context, target,
                                         attached["sessionId"], width, height)
                self.session_map[session.cdp_session_id] = session
                session.call("Page.enable")
                session.call("Runtime.enable")
                session.call("Emulation.setDeviceMetricsOverride", {
                    "width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
                session.call("Page.setInterceptFileChooserDialog", {"enabled": True})
                session.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
                self.call("Browser.setDownloadBehavior", {
                    "behavior": "allow", "downloadPath": str(session.download_dir),
                    "browserContextId": context})
                session.navigate(url)
                return session
            except Exception:
                with contextlib.suppress(Exception):
                    self.call("Target.disposeBrowserContext", {"browserContextId": context})
                raise

    def attach(self, session, target_id):
        targets = self.call("Target.getTargets")["targetInfos"]
        require(any(item.get("targetId") == target_id
                    and item.get("browserContextId") == session.browser_context_id
                    and item.get("type") == "page" for item in targets),
                "目标标签页不属于此会话", "invalid_target")
        attached = self.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session.target_id = target_id
        session.cdp_session_id = attached["sessionId"]
        self.session_map[session.cdp_session_id] = session
        session.call("Page.enable")
        session.call("Runtime.enable")
        session.call("Emulation.setDeviceMetricsOverride", {
            "width": session.width, "height": session.height,
            "deviceScaleFactor": 1, "mobile": False})
        session.call("Page.setInterceptFileChooserDialog", {"enabled": True})
        session.call("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})

    def new_tab(self, session, url):
        navigation_url(url)
        target = self.call("Target.createTarget", {
            "url": "about:blank", "background": True,
            "browserContextId": session.browser_context_id})["targetId"]
        self.attach(session, target)
        session.navigate(url)
        return session

    def tabs(self, session):
        targets = self.call("Target.getTargets")["targetInfos"]
        return [{"id": item.get("targetId"), "title": item.get("title", ""),
                 "url": item.get("url", ""), "active": item.get("targetId") == session.target_id}
                for item in targets if item.get("type") == "page"
                and item.get("browserContextId") == session.browser_context_id]

    def destroy(self, session):
        with self.lock:
            self.call("Target.disposeBrowserContext", {"browserContextId": session.browser_context_id})
            session.closed = True
            for identifier, value in list(self.session_map.items()):
                if value is session:
                    self.session_map.pop(identifier, None)

    def event(self, message):
        method = message.get("method")
        params = message.get("params", {})
        session = self.session_map.get(message.get("sessionId"))
        if method == "Fetch.requestPaused" and session:
            request = params.get("request", {})
            url = request.get("url", "")
            allowed = urllib.parse.urlsplit(url).scheme.lower() in {"http", "https", "about", "data", "blob"}
            action = "Fetch.continueRequest" if allowed else "Fetch.failRequest"
            payload = {"requestId": params["requestId"]}
            if not allowed:
                payload["errorReason"] = "BlockedByClient"
            self.cdp.notify(action, payload, session.cdp_session_id)
            return
        if method in {"Browser.downloadWillBegin", "Page.downloadWillBegin"}:
            owner = session or next((candidate for candidate in self.session_map.values()
                                    if candidate.target_id == params.get("frameId")), None)
            if owner and params.get("frameId") == owner.target_id and params.get("guid"):
                guid = params["guid"]
                self.download_owners[guid] = owner
                owner.downloads[guid] = {
                    "guid": guid, "suggestedFilename": params.get("suggestedFilename", ""),
                    "url": params.get("url", ""), "state": "started",
                }
            return
        if method in {"Browser.downloadProgress", "Page.downloadProgress"}:
            owner = self.download_owners.get(params.get("guid"))
            if owner and params["guid"] in owner.downloads:
                item = owner.downloads[params["guid"]]
                for key in ("state", "receivedBytes", "totalBytes", "filePath"):
                    if key in params:
                        item[key] = params[key]
            return
        if not session:
            return
        if method == "Page.javascriptDialogOpening":
            session.dialog = {"type": params.get("type"), "message": params.get("message", "")}
        elif method == "Page.javascriptDialogClosed":
            session.dialog = None
        elif method == "Target.detachedFromTarget" and session.cdp_session_id == message.get("sessionId"):
            session.closed = True
        elif method == "Target.targetDestroyed" and session.target_id == params.get("targetId"):
            session.closed = True

    def stop(self):
        with self.lock:
            if self.cdp:
                with contextlib.suppress(Exception):
                    self.cdp.call("Browser.close", timeout=5)
                self.cdp.close()
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGTERM)
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        self.process.wait(timeout=2)
            self.cdp = None
            self.process = None
            self.session_map.clear()
            self.download_owners.clear()
            if self.profile:
                self.remove_profile(self.profile)
            self.profile = None
            with contextlib.suppress(FileNotFoundError):
                self.state_path.unlink()

    def remove_profile(self, profile):
        profile = Path(profile)
        require(profile.parent == self.profile_root and profile.name.startswith("profile-"),
                "私有浏览器目录不匹配", "invalid_profile")
        require(not profile.is_symlink(), "私有浏览器目录不能是符号链接", "invalid_profile")
        if profile.exists():
            shutil.rmtree(profile)

    @staticmethod
    def owns_command(command, profile, executable):
        return (command.startswith(executable + " ") and "--headless=new" in command.split()
                and f"--user-data-dir={profile}" in command.split())

    @classmethod
    def recover_orphan(cls, state_dir=STATE_DIR):
        state_path = Path(state_dir) / "browser.json"
        if not state_path.exists():
            return
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BrowserError(f"无法读取遗留浏览器状态：{error}", "invalid_recovery")
        profile = state.get("profile")
        pid = state.get("pid")
        executable = state.get("executable")
        require(isinstance(profile, str) and isinstance(executable, str)
                and Path(profile).parent == Path(state_dir) / "profiles"
                and Path(profile).name.startswith("profile-")
                and (pid is None or isinstance(pid, int) and not isinstance(pid, bool) and pid > 1),
                "遗留浏览器状态无效；未启动新浏览器", "invalid_recovery")
        candidates = [] if pid is None else [pid]
        if pid is None:
            result = subprocess.run(["ps", "-axo", "pid=,command="], text=True,
                                    capture_output=True, timeout=3)
            candidates = [int(line.split(None, 1)[0]) for line in result.stdout.splitlines()
                          if line.strip() and cls.owns_command(line.split(None, 1)[1], profile, executable)]
        for candidate in candidates:
            result = subprocess.run(["ps", "-p", str(candidate), "-o", "command="],
                                    text=True, capture_output=True, timeout=3)
            if not cls.owns_command(result.stdout.strip(), profile, executable):
                continue
            os.kill(candidate, signal.SIGTERM)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                time.sleep(0.05)
                result = subprocess.run(["ps", "-p", str(candidate), "-o", "command="],
                                        text=True, capture_output=True, timeout=3)
                if not cls.owns_command(result.stdout.strip(), profile, executable):
                    break
            else:
                result = subprocess.run(["ps", "-p", str(candidate), "-o", "command="],
                                        text=True, capture_output=True, timeout=3)
                if cls.owns_command(result.stdout.strip(), profile, executable):
                    os.kill(candidate, signal.SIGKILL)
        profile_path = Path(profile)
        if profile_path.exists() and not profile_path.is_symlink():
            shutil.rmtree(profile_path)
        state_path.unlink()


class BrowserSession:
    def __init__(self, browser, session_id, browser_context_id, target_id, cdp_session_id, width, height):
        self.browser = browser
        self.id = session_id
        self.browser_context_id = browser_context_id
        self.target_id = target_id
        self.cdp_session_id = cdp_session_id
        self.width = width
        self.height = height
        self.cursor = {"x": width / 2, "y": height / 2}
        self.dialog = None
        self.closed = False
        self.downloads = {}
        self.last_activity = time.time()
        self.download_dir = Path(browser.state_dir) / "sessions" / session_id / "downloads"
        private_directory(self.download_dir)

    def call(self, method, params=None, timeout=30):
        require(not self.closed, "页面或会话已关闭；请重新读取状态", "session_closed")
        return self.browser.call(method, params, self.cdp_session_id, timeout)

    def evaluate(self, expression):
        bounded_text(expression, "script", 131072)
        response = self.call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True, "userGesture": True})
        if "exceptionDetails" in response:
            raise BrowserError("页面脚本执行失败", "page_script_failed")
        value = response.get("result", {}).get("value")
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        require(len(encoded.encode("utf-8")) <= MAX_REQUEST, "脚本返回值过大", "response_too_large")
        return value

    def info(self):
        target = self.browser.call("Target.getTargetInfo", {"targetId": self.target_id})["targetInfo"]
        return {"title": target.get("title", ""), "url": target.get("url", "")}

    def navigate(self, url):
        navigation_url(url)
        response = self.call("Page.navigate", {"url": url})
        require("errorText" not in response,
                f"页面导航失败：{response.get('errorText', '未知错误')}", "navigation_failed")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.evaluate("document.readyState") == "complete":
                break
            time.sleep(0.1)
        info = self.info()
        return {**info, "load": "complete" if self.evaluate("document.readyState") == "complete" else "timeout"}

    def snapshot(self, max_chars=12000, limit=100, selector=None):
        require(isinstance(max_chars, int) and not isinstance(max_chars, bool) and 1 <= max_chars <= 100000,
                "max-chars 须为 1–100000")
        require(isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= 200,
                "limit 须为 1–200")
        if self.dialog is not None:
            return self._dialog_state()
        try:
            return self._snapshot(max_chars, limit, selector)
        except BrowserError as error:
            if error.code == "dialog_pending" and self.dialog is not None:
                return self._dialog_state()
            raise

    def _dialog_state(self):
        return {**self.info(), "text": "", "elements": [], "cursor": self.cursor,
                "dialog": self.dialog, "route": "managed_headless"}

    def _snapshot(self, max_chars=12000, limit=100, selector=None):
        if selector is not None:
            bounded_text(selector, "selector")
            text = self.evaluate(f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
                                 "if (!el) return null; return el.type === 'password' ? '[redacted]'"
                                 " : (el.value ?? el.innerText ?? el.textContent ?? ''); })()")
            require(text is not None, "没有找到指定元素", "element_not_found")
            return {"text": str(text)[:max_chars], "redacted": "[redacted]" == text}
        result = self.evaluate(f"""(() => {{
          const visible = el => {{ const r = el.getBoundingClientRect();
            const s = getComputedStyle(el); return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; }};
          const label = el => (el.getAttribute('aria-label') || (el.type === 'password' ? '' : el.value) ||
            el.innerText || el.placeholder || el.title || '').replace(/\\s+/g, ' ').trim().slice(0, 300);
          const css = el => {{ if (el.id) return '#' + CSS.escape(el.id);
            const parts = []; let current = el;
            while (current && current !== document.body) {{
              const parent = current.parentElement; if (!parent) break;
              const same = Array.from(parent.children).filter(x => x.tagName === current.tagName);
              parts.unshift(current.tagName.toLowerCase() + ':nth-of-type(' + (same.indexOf(current) + 1) + ')');
              current = parent;
            }} return parts.join(' > ');
          }};
          const nodes = Array.from(document.querySelectorAll('a,button,input,textarea,select,[role="button"],[contenteditable="true"]'))
            .filter(visible).slice(0, {limit}).map(el => {{
              const r = el.getBoundingClientRect();
              return {{tag:el.tagName.toLowerCase(),text:label(el),id:el.id || null,
                role:el.getAttribute('role'),selector:css(el),x:r.x,y:r.y,width:r.width,height:r.height,
                disabled:!!el.disabled,sensitive:el.type === 'password'}};
            }});
          return {{title:document.title,url:location.href,text:(document.body?.innerText || '').slice(0,{max_chars}),elements:nodes}};
        }})()""")
        return {**result, "cursor": self.cursor, "dialog": self.dialog, "route": "managed_headless"}

    def point(self, x, y):
        return {"x": coordinate(x, self.width, "x"), "y": coordinate(y, self.height, "y")}

    def center(self, selector=None, text=None):
        if selector is not None:
            bounded_text(selector, "selector")
            finder = f"Array.from(document.querySelectorAll({json.dumps(selector)}))"
        else:
            bounded_text(text, "text", 512)
            finder = r"""Array.from(document.querySelectorAll('a,button,input,textarea,select,[role="button"],[contenteditable="true"]'))
              .filter(el => { const value = (el.getAttribute('aria-label') || el.value || el.innerText || el.placeholder || '').replace(/\s+/g,' ').trim();
                return value === TEXT; })"""
            exact = finder.replace("TEXT", json.dumps(text))
            fallback = finder.replace("value === TEXT", f"value.includes({json.dumps(text)})")
            finder = f"(() => {{ const exact = {exact}; return exact.length ? exact : {fallback}; }})()"
        expression = f"""(() => {{
          const candidates = {finder}.filter(el => {{
            const r = el.getBoundingClientRect(); const style = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
          }});
          if (candidates.length !== 1) return {{error:candidates.length ? 'ambiguous' : 'missing'}};
          const el = candidates[0];
          if (el.disabled || el.getAttribute('aria-disabled') === 'true') return {{error:'disabled'}};
          const href = el.getAttribute('href') || '';
          if (/^(mailto|tel|file|ftp|itms-services|intent):/i.test(href)) return {{error:'external_protocol'}};
          el.scrollIntoView({{block:'center',inline:'center',behavior:'instant'}});
          return new Promise(resolve => requestAnimationFrame(() => {{
            const r = el.getBoundingClientRect(); resolve({{x:r.x+r.width/2,y:r.y+r.height/2}});
          }}));
        }})()"""
        result = self.evaluate(expression)
        require(isinstance(result, dict) and "error" not in result,
                {"missing": "没有找到唯一可见元素", "ambiguous": "匹配到多个元素；请用更精确的选择器或文本",
                 "disabled": "目标元素当前不可用", "external_protocol": "外部协议不会打开其他应用"}.get(
                     result.get("error") if isinstance(result, dict) else None, "元素坐标无效"),
                "element_not_found" if isinstance(result, dict) and result.get("error") == "missing" else "element_rejected")
        return self.point(result["x"], result["y"])

    def mouse(self, kind, point, extra=None):
        return self.call("Input.dispatchMouseEvent", {
            "type": kind, "x": point["x"], "y": point["y"], **(extra or {})})

    def click(self, selector=None, text=None, x=None, y=None):
        point = self.point(x, y) if x is not None else self.center(selector, text)
        self.mouse("mouseMoved", point)
        self.mouse("mousePressed", point, {"button": "left", "buttons": 1, "clickCount": 1})
        self.mouse("mouseReleased", point, {"button": "left", "buttons": 0, "clickCount": 1})
        self.cursor = point
        return {"pointer": point, "delivery": "devtools"}

    def move(self, x, y):
        point = self.point(x, y)
        self.mouse("mouseMoved", point)
        self.cursor = point
        return {"pointer": point}

    def scroll(self, dx, dy):
        require(isinstance(dx, int) and isinstance(dy, int) and not isinstance(dx, bool)
                and not isinstance(dy, bool) and -10000 <= dx <= 10000 and -10000 <= dy <= 10000
                and (dx or dy), "滚动量须为非零的 -10000–10000 整数")
        self.mouse("mouseWheel", self.cursor, {"deltaX": dx, "deltaY": dy})
        return {"pointer": self.cursor, "delivery": "devtools"}

    def focus(self, selector, replace=False):
        bounded_text(selector, "selector")
        result = self.evaluate(f"""(() => {{
          const all = Array.from(document.querySelectorAll({json.dumps(selector)}));
          if (all.length !== 1) return false;
          const el = all[0]; if (el.disabled || el.readOnly) return false;
          el.focus();
          if ({json.dumps(replace)}) {{
            if (typeof el.select === 'function') {{ try {{ el.select(); }} catch (_) {{}} }}
            else if (el.isContentEditable) {{
              const range = document.createRange(); range.selectNodeContents(el);
              const selection = getSelection(); selection.removeAllRanges(); selection.addRange(range);
            }}
          }}
          return document.activeElement === el;
        }})()""")
        require(result is True, "无法聚焦唯一的可编辑元素", "element_rejected")

    def type(self, selector, text, replace=False):
        bounded_text(text, "text", 131072, allow_empty=True)
        require(isinstance(replace, bool), "replace 必须是布尔值")
        self.focus(selector, replace)
        self.call("Input.insertText", {"text": text})
        return {"inserted": len(text), "delivery": "devtools", "confirmation": "read_back_required"}

    def key(self, combo):
        bounded_text(combo, "key", 64)
        parts = combo.lower().split("+")
        name = parts[-1]
        modifiers = set(parts[:-1])
        require(all(part in {"cmd", "command", "meta", "ctrl", "control", "alt", "shift"} for part in modifiers),
                "不支持的修饰键")
        require(not (modifiers & {"cmd", "command", "meta", "ctrl", "control"} and name in {"c", "x", "v"}),
                "不读取或修改系统剪贴板；请使用 type 直接输入")
        keys = {
            "enter": ("Enter", "Enter", 13, "\r"), "return": ("Enter", "Enter", 13, "\r"),
            "tab": ("Tab", "Tab", 9, ""), "esc": ("Escape", "Escape", 27, ""),
            "escape": ("Escape", "Escape", 27, ""), "backspace": ("Backspace", "Backspace", 8, ""),
            "delete": ("Delete", "Delete", 46, ""), "space": (" ", "Space", 32, " "),
            "left": ("ArrowLeft", "ArrowLeft", 37, ""), "right": ("ArrowRight", "ArrowRight", 39, ""),
            "up": ("ArrowUp", "ArrowUp", 38, ""), "down": ("ArrowDown", "ArrowDown", 40, ""),
        }
        if len(name) == 1 and name.isalnum():
            key, code, virtual = name, "Key" + name.upper() if name.isalpha() else "Digit" + name, ord(name.upper())
        else:
            require(name in keys, "支持的按键为 enter、tab、esc、delete、space、方向键和字母数字")
            key, code, virtual, text = keys[name]
        flags = (4 if modifiers & {"cmd", "command", "meta"} else 0) | \
                (2 if modifiers & {"ctrl", "control"} else 0) | \
                (1 if "alt" in modifiers else 0) | (8 if "shift" in modifiers else 0)
        params = {"key": key, "code": code, "windowsVirtualKeyCode": virtual,
                  "nativeVirtualKeyCode": virtual, "modifiers": flags}
        if len(name) == 1:
            params["text"] = name.upper() if "shift" in modifiers else name
        elif key in {"Enter", " ", "Tab"}:
            params["text"] = keys[name][3] if name in keys else ("\r" if key == "Enter" else "")
        for kind in ("rawKeyDown", "keyUp"):
            self.call("Input.dispatchKeyEvent", {"type": kind, **params})
        return {"key": combo, "delivery": "devtools"}

    def frame(self, raw=False):
        require(isinstance(raw, bool), "raw 必须是布尔值")
        result = self.call("Page.captureScreenshot", {"format": "png", "fromSurface": True})
        data = base64.b64decode(result["data"], validate=True)
        require(0 < len(data) <= MAX_IMAGE, "截图为空或超过大小限制", "invalid_image")
        return data if raw else annotate_cursor(data, self.cursor, (self.width, self.height))

    def handle_dialog(self, accept, text=None):
        require(isinstance(accept, bool), "accept 必须是布尔值")
        require(self.dialog is not None, "当前没有 JavaScript 对话框", "no_dialog")
        params = {"accept": accept}
        if text is not None:
            bounded_text(text, "text", 131072, allow_empty=True)
            params["promptText"] = text
        self.call("Page.handleJavaScriptDialog", params)
        self.dialog = None
        return {"handled": True}

    def upload(self, selector, files):
        bounded_text(selector, "selector")
        require(isinstance(files, list) and 1 <= len(files) <= 8, "一次最多上传 8 个文件")
        checked = [validate_upload(path) for path in files]
        require(sum(Path(path).stat().st_size for path in checked) <= 200 * 1024 * 1024,
                "一次上传总量不得超过 200 MiB")
        document = self.call("DOM.getDocument", {"depth": 1})
        nodes = self.call("DOM.querySelectorAll", {
            "nodeId": document["root"]["nodeId"], "selector": selector})["nodeIds"]
        require(len(nodes) == 1, "文件输入框必须唯一", "element_rejected")
        self.call("DOM.setFileInputFiles", {"nodeId": nodes[0], "files": checked})
        return {"attached": len(checked)}

    def download_info(self):
        self.info()
        result = []
        for item in list(self.downloads.values())[-32:]:
            row = {key: value for key, value in item.items() if key != "filePath"}
            if item.get("state") != "completed":
                result.append(row)
                continue
            candidate = None
            path = item.get("filePath")
            if isinstance(path, str):
                supplied = Path(path)
                if supplied.is_file() and not supplied.is_symlink() and supplied.parent == self.download_dir:
                    candidate = supplied
            if candidate is None:
                filename = Path(item.get("suggestedFilename", "")).name
                if filename:
                    stem = Path(filename).stem
                    suffix = Path(filename).suffix
                    matches = [child for child in self.download_dir.iterdir()
                               if child.is_file() and not child.is_symlink()
                               and (child.name == filename
                                    or child.stem.startswith(stem + " (") and child.suffix == suffix)]
                    if matches:
                        candidate = max(matches, key=lambda child: child.stat().st_mtime)
            if candidate is not None and candidate.parent == self.download_dir:
                row["path"] = str(candidate)
                row["bytes"] = candidate.stat().st_size
            result.append(row)
        return result


def validate_upload(value):
    bounded_text(value, "file", 4096)
    require(value.startswith("/"), "上传文件必须使用绝对路径")
    path = Path(value)
    name = path.name.lower()
    require(name not in SENSITIVE_FILES and path.suffix.lower() not in SENSITIVE_SUFFIXES
            and not name.startswith(".env"), "不上传钥匙、凭据或环境秘密文件")
    require(path.is_file() and not path.is_symlink(), "上传文件必须是普通文件且不能是符号链接")
    require(stat.S_ISREG(path.stat().st_mode) and path.stat().st_size <= 100 * 1024 * 1024,
            "单个上传文件不得超过 100 MiB")
    return str(path)


def annotate_cursor(data, cursor, viewport):
    source = AppKit.NSImage.alloc().initWithData_(data)
    if source is None:
        raise BrowserError("浏览器返回了无效 PNG", "invalid_image")
    width, height = int(source.size().width), int(source.size().height)
    require(width > 0 and height > 0 and width * height <= 20_000_000,
            "截图尺寸超过限制", "invalid_image")
    image = AppKit.NSImage.alloc().initWithSize_((width, height))
    image.lockFocus()
    source.drawInRect_fromRect_operation_fraction_(
        ((0, 0), (width, height)), AppKit.NSZeroRect, AppKit.NSCompositingOperationSourceOver, 1.0)
    scale = min(width / viewport[0], height / viewport[1])
    x = float(cursor["x"]) * width / viewport[0] - 2 * scale
    y = height - float(cursor["y"]) * height / viewport[1] + 30 * scale
    points = [(2, 30), (2, 7), (8, 13), (12, 4), (17, 7), (13, 16), (21, 16)]
    path = AppKit.NSBezierPath.bezierPath()
    for index, (dx, dy) in enumerate(points):
        point = (x + dx * scale, y - dy * scale)
        if index == 0:
            path.moveToPoint_(point)
        else:
            path.lineToPoint_(point)
    path.closePath()
    AppKit.NSColor.systemBlueColor().setFill()
    path.fill()
    AppKit.NSColor.blackColor().setStroke()
    path.setLineWidth_(4 * scale)
    path.stroke()
    AppKit.NSColor.whiteColor().setStroke()
    path.setLineWidth_(1.6 * scale)
    path.stroke()
    image.unlockFocus()
    representation = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
    result = representation.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {})
    require(result is not None and len(result) <= MAX_IMAGE, "光标合成后的截图过大", "invalid_image")
    return bytes(result)


class Daemon:
    def __init__(self, state_dir=STATE_DIR, browser=None, viewer_html=None, session_ttl=3600):
        self.state_dir = Path(state_dir)
        self.browser = browser or Chrome(self.state_dir)
        self.sessions = {}
        self.leases = {}
        self.session_ttl = session_ttl
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.viewer_token = secrets.token_urlsafe(32)
        self.tickets = {}
        self.viewer_html = viewer_html if viewer_html is not None else VIEWER_HTML.read_text(encoding="utf-8")
        self.httpd = None
        self.viewer_thread = None
        self.listener = None
        self.lock_handle = None

    def doctor(self):
        return {"running": True, "route": "managed_headless", "headless": True,
                "chrome_running": self.browser.running, "sessions": sorted(self.sessions),
                "viewer": "agent-browser show --session ID", "version": VERSION}

    def create(self, params):
        session_id = valid_session(params.get("session"))
        with self.lock:
            require(session_id not in self.sessions, "session 已存在；请换名称或先 destroy", "session_exists")
            session = self.browser.create(session_id, params.get("width", 1280), params.get("height", 900),
                                         params.get("url", "about:blank"))
            lease = secrets.token_urlsafe(32)
            self.sessions[session_id] = session
            self.leases[session_id] = lease
            return {"session": session_id, "lease": lease, "route": "managed_headless",
                    "viewport": {"width": session.width, "height": session.height},
                    "cursor": session.cursor, "viewer": f"agent-browser show --session {session_id}"}

    def authorize(self, params):
        session_id = valid_session(params.get("session"))
        session = self.sessions.get(session_id)
        lease = params.get("lease")
        require(session is not None and not session.closed, "没有该会话", "unknown_session")
        expected = self.leases.get(session_id, "")
        require(isinstance(lease, str) and hmac.compare_digest(lease, expected),
                "session lease 不匹配", "invalid_lease")
        session.last_activity = time.time()
        return session

    def destroy(self, params):
        session = self.authorize(params)
        self.browser.destroy(session)
        self.sessions.pop(session.id, None)
        self.leases.pop(session.id, None)
        if not self.sessions:
            self.browser.stop()
        return {"session": session.id, "destroyed": True}

    def handle(self, request):
        command = request.get("cmd")
        params = request.get("params", {})
        require(isinstance(command, str) and isinstance(params, dict), "请求格式无效")
        if command == "doctor":
            return self.doctor()
        if command == "create":
            return self.create(params)
        if command == "destroy":
            return self.destroy(params)
        if command == "daemon.stop":
            require(not self.sessions, "仍有活动会话；请先 destroy，不停止其他智能体的会话", "sessions_active")
            threading.Timer(0.1, self.stop_event.set).start()
            return {"stopping": True}
        session = self.authorize(params)
        if command == "heartbeat":
            return {"session": session.id, "heartbeat": True}
        if command == "navigate":
            return session.navigate(params.get("url"))
        if command == "snapshot":
            return session.snapshot(params.get("max_chars", 12000), params.get("limit", 100),
                                    params.get("selector"))
        if command == "click":
            has_text = params.get("text") is not None
            has_selector = params.get("selector") is not None
            has_point = params.get("x") is not None or params.get("y") is not None
            require(sum((has_text, has_selector, has_point)) == 1 and (not has_point or
                    params.get("x") is not None and params.get("y") is not None),
                    "click 需要唯一的 selector、text 或完整 x/y")
            return session.click(params.get("selector"), params.get("text"), params.get("x"), params.get("y"))
        if command == "type":
            return session.type(params.get("selector"), params.get("text"), params.get("replace", False))
        if command == "key":
            return session.key(params.get("key"))
        if command == "scroll":
            return session.scroll(params.get("dx", 0), params.get("dy", 0))
        if command == "move":
            return session.move(params.get("x"), params.get("y"))
        if command == "evaluate":
            return {"value": session.evaluate(params.get("script"))}
        if command == "tabs":
            return {"tabs": self.browser.tabs(session)}
        if command == "new-tab":
            self.browser.new_tab(session, params.get("url"))
            return {"tabs": self.browser.tabs(session)}
        if command == "activate-tab":
            self.browser.attach(session, params.get("target"))
            return {"tabs": self.browser.tabs(session)}
        if command == "dialog":
            return session.handle_dialog(params.get("accept", False), params.get("text"))
        if command == "upload":
            return session.upload(params.get("selector"), params.get("files"))
        if command == "downloads":
            return {"downloads": session.download_info()}
        if command == "screenshot":
            return {"png": base64.b64encode(session.frame(params.get("raw", False))).decode("ascii"),
                    "cursor": session.cursor}
        if command == "show":
            ticket = secrets.token_urlsafe(32)
            self.tickets[ticket] = (session.id, time.monotonic() + 30)
            query = urllib.parse.urlencode({"session": session.id, "ticket": ticket})
            return {"session": session.id, "url": f"http://127.0.0.1:{self.httpd.server_port}/open?{query}"}
        raise BrowserError("未知浏览器命令")

    def viewer_authorized(self, headers):
        try:
            cookie = http.cookies.SimpleCookie(headers.get("Cookie", ""))
            token = cookie["agent_browser_viewer"].value
        except (KeyError, http.cookies.CookieError):
            return False
        return hmac.compare_digest(token, self.viewer_token)

    def viewer_session(self, session_id):
        valid_session(session_id)
        session = self.sessions.get(session_id)
        require(session is not None and not session.closed, "会话已结束", "unknown_session")
        return session

    def viewer_frame(self, session_id):
        session = self.viewer_session(session_id)
        return {**session.info(), "cursor": session.cursor,
                "viewport": {"width": session.width, "height": session.height},
                "png": base64.b64encode(session.frame()).decode("ascii")}

    def viewer_input(self, session_id, action):
        session = self.viewer_session(session_id)
        kind = action.get("kind")
        if kind == "click":
            return session.click(x=action.get("x"), y=action.get("y"))
        if kind == "move":
            return session.move(action.get("x"), action.get("y"))
        if kind == "scroll":
            return session.scroll(action.get("dx", 0), action.get("dy", 0))
        if kind == "type":
            text = action.get("text")
            bounded_text(text, "text", 131072, allow_empty=True)
            session.call("Input.insertText", {"text": text})
            return {"inserted": len(text)}
        if kind == "key":
            return session.key(action.get("key"))
        raise BrowserError("观察页不支持的操作")

    def expire_tickets(self):
        with self.lock:
            now = time.monotonic()
            for ticket, (_, expires) in list(self.tickets.items()):
                if expires < now:
                    self.tickets.pop(ticket, None)
            if len(self.tickets) > 32:
                self.tickets.pop(next(iter(self.tickets)), None)

    def janitor(self):
        now = time.time()
        for session_id, session in list(self.sessions.items()):
            if now - session.last_activity < self.session_ttl:
                continue
            lease = self.leases[session_id]
            self.destroy({"session": session_id, "lease": lease})

    def serve(self):
        private_directory(self.state_dir)
        self.lock_handle = open(self.state_dir / "daemon.lock", "a+")
        try:
            fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BrowserError("agent-browser daemon 已在运行", "daemon_running")
        Chrome.recover_orphan(self.state_dir)
        self.httpd = make_viewer_server(self)
        self.viewer_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.viewer_thread.start()
        if SOCKET_PATH.exists():
            require(stat.S_ISSOCK(SOCKET_PATH.lstat().st_mode),
                    "后台 socket 路径不是 socket；拒绝覆盖", "invalid_socket")
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.3)
                probe.connect(str(SOCKET_PATH))
                raise BrowserError("agent-browser daemon 已在运行", "daemon_running")
            except ConnectionRefusedError:
                SOCKET_PATH.unlink()
            finally:
                probe.close()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        self.listener.listen(16)
        self.listener.settimeout(0.5)
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: self.stop_event.set())
        print("agent-browser daemon ready: managed headless browser", flush=True)
        last_janitor = time.monotonic()
        try:
            while not self.stop_event.wait(0.2):
                if time.monotonic() - last_janitor >= 5:
                    self.janitor()
                    last_janitor = time.monotonic()
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.serve_client, args=(connection,), daemon=True).start()
        finally:
            self.listener.close()
            with contextlib.suppress(FileNotFoundError):
                SOCKET_PATH.unlink()
            self.httpd.shutdown()
            self.httpd.server_close()
            self.viewer_thread.join(timeout=2)
            self.browser.stop()
            self.lock_handle.close()

    def serve_client(self, connection):
        try:
            if hasattr(connection, "getpeereid") and connection.getpeereid()[0] != os.getuid():
                return
            connection.settimeout(35)
            request = receive_json(connection, MAX_REQUEST)
            result = self.handle(request)
            send_json(connection, {"ok": True, "result": result})
        except BrowserError as error:
            with contextlib.suppress(Exception):
                send_json(connection, {"ok": False, "error": str(error), "errorCode": error.code})
        except (OSError, ValueError, TypeError) as error:
            with contextlib.suppress(Exception):
                send_json(connection, {
                    "ok": False, "error": f"浏览器后台未能完成请求（{type(error).__name__}）",
                    "errorCode": "internal_error"})
        finally:
            connection.close()


def make_viewer_server(daemon):
    class ViewerServer(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = False

    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "AgentBrowserViewer/1.0"
        sys_version = ""

        def log_message(self, *args):
            return

        def local_request(self):
            host = self.headers.get("Host", "")
            return host in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def reply(self, status, body, content_type="application/json; charset=utf-8", headers=None):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            site = self.headers.get("Sec-Fetch-Site")
            if site not in {None, "same-origin"}:
                return False
            origin = self.headers.get("Origin")
            return not origin or origin in {
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            }

        def do_GET(self):
            if not self.local_request():
                self.reply(403, {"error": "forbidden"})
                return
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path == "/":
                body = daemon.viewer_html.encode("utf-8")
                self.reply(200, body, "text/html; charset=utf-8", {
                    "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; "
                    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; "
                    "base-uri 'none'; form-action 'none'"})
                return
            if parsed.path == "/open":
                query = urllib.parse.parse_qs(parsed.query, max_num_fields=8)
                ticket = query.get("ticket", [""])[0]
                session_id = query.get("session", [""])[0]
                with daemon.lock:
                    daemon.expire_tickets()
                    matched = next((candidate for candidate in daemon.tickets
                                    if ticket.isascii() and hmac.compare_digest(candidate, ticket)), None)
                    valid = matched is not None and daemon.tickets[matched][0] == session_id
                    if valid:
                        daemon.tickets.pop(matched, None)
                if not valid:
                    self.reply(403, {"error": "expired viewer ticket"})
                    return
                self.reply(303, {"ok": True}, headers={
                    "Location": "/?" + urllib.parse.urlencode({"session": session_id}),
                    "Set-Cookie": f"agent_browser_viewer={daemon.viewer_token}; HttpOnly; SameSite=Strict; Path=/",
                })
                return
            if parsed.path == "/api/state" and self.authorized() and daemon.viewer_authorized(self.headers):
                session_id = urllib.parse.parse_qs(parsed.query).get("session", [""])[0]
                try:
                    self.reply(200, daemon.viewer_frame(session_id))
                except BrowserError as error:
                    self.reply(404, {"error": str(error)})
                return
            self.reply(403, {"error": "forbidden"})

        def do_POST(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/api/input" or not self.local_request() or not self.authorized() \
                    or not daemon.viewer_authorized(self.headers):
                self.reply(403, {"error": "forbidden"})
                return
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self.reply(415, {"error": "JSON required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 131072:
                    raise ValueError
                action = json.loads(self.rfile.read(length))
                if not isinstance(action, dict):
                    raise ValueError
                session_id = urllib.parse.parse_qs(parsed.query).get("session", [""])[0]
                self.reply(200, daemon.viewer_input(session_id, action))
            except (ValueError, TypeError, json.JSONDecodeError, BrowserError):
                self.reply(400, {"error": "invalid viewer input"})

    return ViewerServer(("127.0.0.1", 0), Handler)


def create_session(args):
    require(not client_path(args.session).exists(), f"session {args.session} 已存在；请先 destroy",
            "session_exists")
    result = remote("create", {"session": args.session, "url": args.url,
                               "width": args.width, "height": args.height})
    try:
        save_client(args.session, result["lease"])
    except Exception:
        with contextlib.suppress(Exception):
            remote("destroy", {"session": args.session, "lease": result["lease"]})
        raise
    result.pop("lease", None)
    emit(result)


def write_screenshot(args):
    result = leased(args.session, "screenshot", {"raw": args.raw})
    data = base64.b64decode(result["png"], validate=True)
    require(len(data) <= MAX_IMAGE, "截图超过大小限制", "invalid_image")
    path = Path(args.output).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    require(not path.is_symlink(), "输出路径不能是符号链接")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if args.replace else os.O_EXCL)
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    emit({"session": args.session, "path": str(path), "cursor": result.get("cursor"),
          "bytes": len(data), "raw": args.raw})


def show_session(args):
    result = leased(args.session, "show")
    url = result.get("url", "")
    parts = urllib.parse.urlsplit(url)
    require(parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.path == "/open",
            "观察页地址无效", "invalid_response")
    opened = AppKit.NSWorkspace.sharedWorkspace().openURL_(AppKit.NSURL.URLWithString_(url))
    require(opened, "无法打开观察页；请检查默认浏览器")
    emit({"session": args.session, "viewer_opened": True, "control": "off_by_default"})


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    daemon_parser = sub.add_parser("daemon")
    daemon_parser.add_argument("action", nargs="?", choices=["start", "stop"], default="start")
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--session", required=True)
    create_parser.add_argument("--url", default="about:blank")
    create_parser.add_argument("--width", type=int, default=1280)
    create_parser.add_argument("--height", type=int, default=900)
    for name in ["navigate", "new-tab"]:
        item = sub.add_parser(name)
        item.add_argument("--session", required=True)
        item.add_argument("--url", required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--session", required=True)
    snapshot.add_argument("--max-chars", type=int, default=12000)
    snapshot.add_argument("--limit", type=int, default=100)
    snapshot.add_argument("--selector")
    click = sub.add_parser("click")
    click.add_argument("--session", required=True)
    click.add_argument("--selector")
    click.add_argument("--text")
    click.add_argument("--x", type=float)
    click.add_argument("--y", type=float)
    typing = sub.add_parser("type")
    typing.add_argument("--session", required=True)
    typing.add_argument("--selector", required=True)
    typing.add_argument("--text", required=True)
    typing.add_argument("--replace", action="store_true")
    key = sub.add_parser("key")
    key.add_argument("--session", required=True)
    key.add_argument("--key", required=True)
    scroll = sub.add_parser("scroll")
    scroll.add_argument("--session", required=True)
    scroll.add_argument("--dx", type=int, default=0)
    scroll.add_argument("--dy", type=int, default=0)
    move = sub.add_parser("move")
    move.add_argument("--session", required=True)
    move.add_argument("--x", type=float, required=True)
    move.add_argument("--y", type=float, required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--session", required=True)
    evaluate.add_argument("--script", required=True)
    tabs = sub.add_parser("tabs")
    tabs.add_argument("--session", required=True)
    activate = sub.add_parser("activate-tab")
    activate.add_argument("--session", required=True)
    activate.add_argument("--target", required=True)
    dialog = sub.add_parser("dialog")
    dialog.add_argument("--session", required=True)
    dialog.add_argument("--accept", action="store_true")
    dialog.add_argument("--text")
    upload = sub.add_parser("upload")
    upload.add_argument("--session", required=True)
    upload.add_argument("--selector", required=True)
    upload.add_argument("--files", nargs="+", required=True)
    downloads = sub.add_parser("downloads")
    downloads.add_argument("--session", required=True)
    screenshot = sub.add_parser("screenshot")
    screenshot.add_argument("--session", required=True)
    screenshot.add_argument("--output", "-o", required=True)
    screenshot.add_argument("--raw", action="store_true")
    screenshot.add_argument("--replace", action="store_true")
    for name in ["show", "heartbeat", "destroy"]:
        item = sub.add_parser(name)
        item.add_argument("--session", required=True)
    return root


def main():
    args = parser().parse_args()
    if args.cmd == "doctor":
        try:
            emit(remote("doctor"))
            return 0
        except OSError:
            emit({"running": False, "next": "agent-browser daemon", "route": "managed_headless"})
            return 1
    if args.cmd == "daemon":
        if args.action == "stop":
            emit(remote("daemon.stop"))
        else:
            Daemon().serve()
        return 0
    if args.cmd == "create":
        create_session(args)
        return 0
    if args.cmd == "screenshot":
        write_screenshot(args)
        return 0
    if args.cmd == "show":
        show_session(args)
        return 0
    if args.cmd == "destroy":
        try:
            emit(leased(args.session, "destroy"))
        except BrowserError as error:
            if error.code != "unknown_session":
                raise
            remove_client(args.session)
            emit({"session": args.session, "destroyed": True, "already_absent": True})
        else:
            remove_client(args.session)
        return 0
    if args.cmd == "upload":
        args.files = [str(Path(value).expanduser() if Path(value).is_absolute()
                         else Path.cwd() / Path(value)) for value in args.files]
    parameters = {key: value for key, value in vars(args).items() if key not in {"cmd", "session"}}
    emit(leased(args.session, args.cmd, parameters))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BrowserError as error:
        print(json.dumps({"ok": False, "error": str(error), "errorCode": error.code},
                         ensure_ascii=False), file=__import__("sys").stderr)
        raise SystemExit(1)
