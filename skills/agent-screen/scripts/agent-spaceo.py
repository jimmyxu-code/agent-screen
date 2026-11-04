#!/usr/bin/env python3
"""Shared, non-MCP lifecycle for SpaceO sessions. Keeps leases out of logs and chat."""

import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SPACEO_BIN = os.environ.get("SPACEO_BIN", os.path.expanduser("~/.local/bin/spaceo"))
AGENT_SCREEN_BIN = os.environ.get("AGENT_SCREEN_BIN", os.path.expanduser("~/.local/bin/agent-screen"))
STATE_DIR = Path(os.environ.get("AGENT_SPACEO_STATE_DIR", os.path.expanduser("~/.local/state/agent-spaceo")))
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
UUID_TEXT = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
LEASE_KEYS = {"lease", "leaseid", "leaseuuid", "controllerleaseid"}


def fail(message, code=1):
    raise RuntimeError(message) if code == 1 else SystemExit(code)


def invoke(arguments):
    try:
        return subprocess.run([SPACEO_BIN, *arguments], text=True, capture_output=True, timeout=120)
    except FileNotFoundError:
        raise SystemExit("找不到 SpaceO；请按 agent-screen 安装说明安装包含会话光标的配套构建")


def emit(result):
    if result.stdout:
        print(UUID_TEXT.sub("<redacted>", result.stdout), end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(UUID_TEXT.sub("<redacted>", result.stderr), file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    return result.returncode


def json_result(result):
    if result.returncode:
        emit(result)
        raise SystemExit(result.returncode)
    try:
        return json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise SystemExit(f"SpaceO 返回了无效 JSON：{error}")


def sanitized(value):
    if isinstance(value, dict):
        return {key: sanitized(item) for key, item in value.items() if key.lower() not in LEASE_KEYS}
    if isinstance(value, list):
        return [sanitized(item) for item in value]
    return value


def state_path(session):
    if not SESSION_ID.fullmatch(session):
        raise SystemExit("session 名称须为 1–64 位字母、数字、点、下划线或短横线")
    return STATE_DIR / f"{session}.json"


def write_state(session, lease, display_id, tile):
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    path = state_path(session)
    if path.exists():
        raise SystemExit(f"session {session} 已有本地 lease，不能覆盖；请换名称或先 destroy")
    payload = {
        "version": 1,
        "session": session,
        "lease": lease,
        "display_id": display_id,
        "tile": tile,
        "created_at": time.time(),
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    os.chmod(path, 0o600)


def load_state(session):
    path = state_path(session)
    if not path.is_file():
        raise SystemExit(f"没有 session {session} 的本地 lease；请先 create")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"session {session} 的状态无法读取：{error}")
    if payload.get("version") != 1 or payload.get("session") != session:
        raise SystemExit(f"session {session} 的状态不匹配")
    lease = payload.get("lease")
    display_id = payload.get("display_id")
    tile = payload.get("tile")
    if not isinstance(lease, str) or not lease or not isinstance(display_id, int) or display_id <= 0:
        raise SystemExit(f"session {session} 的状态缺少有效 lease 或 display")
    if not isinstance(tile, dict):
        raise SystemExit(f"session {session} 的状态缺少 tile")
    return payload


def remove_state(session):
    try:
        state_path(session).unlink()
    except FileNotFoundError:
        pass


def heartbeat(session):
    state = load_state(session)
    result = invoke(["session", "heartbeat", "--session", session, "--lease", state["lease"], "--json"])
    response = json_result(result)
    replacement = response.get("controllerLeaseID")
    if isinstance(replacement, str) and replacement:
        state["lease"] = replacement
        path = state_path(session)
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    return state


def require_shared_pool():
    response = json_result(invoke(["pool", "--json"]))
    displays = response.get("displays") if isinstance(response, dict) else None
    if not isinstance(displays, list):
        raise SystemExit("SpaceO 返回了无效的共享显示池状态")
    occupied = []
    for display in displays:
        if not isinstance(display, dict):
            raise SystemExit("SpaceO 返回了无效的共享显示池状态")
        capacity, used = display.get("capacity"), display.get("used")
        if (not isinstance(capacity, int) or not isinstance(used, int)
                or capacity < 1 or used < 0 or used > capacity):
            raise SystemExit("SpaceO 返回了无效的共享显示池状态")
        if used:
            occupied.append((capacity, used))
    if not occupied:
        return
    if len(occupied) != 1:
        raise SystemExit("已有多个使用中的虚拟屏；为避免重排坐标，不再新增显示")
    capacity, used = occupied[0]
    if capacity == 1:
        raise SystemExit("当前虚拟屏为独占布局；为避免重排坐标，不再新增显示")
    if used >= capacity:
        raise SystemExit("共享虚拟屏任务区域已满；请先结束不再使用的会话，不新增显示")


@contextlib.contextmanager
def shared_creation_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    path = STATE_DIR / "create.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as handle:
        deadline = time.monotonic() + 15
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SystemExit("另一个共享会话仍在创建中；请稍后重试")
                time.sleep(0.1)
        yield


def require_single_physical_screen():
    try:
        result = subprocess.run([AGENT_SCREEN_BIN, "prepare"], text=True,
                                capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SystemExit(f"无法确认物理屏路由，未创建虚拟屏：{error}")
    if result.returncode:
        raise SystemExit("agent-screen prepare 失败，未创建虚拟屏；请先修复屏幕路由")
    try:
        route = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as error:
        raise SystemExit(f"agent-screen 返回了无效路由：{error}")
    if not isinstance(route, dict):
        raise SystemExit("agent-screen 返回了无效路由；未创建虚拟屏")
    if (route.get("mode") == "second_physical_screen"
            and route.get("target_kind") == "built_in"
            and route.get("target_built_in") is True
            and route.get("virtual_screen_required") is False):
        raise SystemExit("当前已有外接物理屏，本机屏就是智能体工作屏；"
                         "请运行 agent-screen prepare 并使用 agent-screen claim PID，不创建 SpaceO 虚拟屏")
    if (route.get("mode") != "single_physical_screen"
            or route.get("target_kind") != "spaceo_virtual"
            or route.get("virtual_screen_required") is not True):
        raise SystemExit("物理屏路由不完整或不一致，未创建虚拟屏；请运行 agent-screen prepare")
    return route


def require_compatible_daemon():
    result = invoke(["doctor", "--json"])
    report = json_result(result)
    daemon = report.get("daemon") if isinstance(report, dict) else None
    capabilities = report.get("capabilities") if isinstance(report, dict) else None
    if not isinstance(daemon, dict) or not isinstance(capabilities, list):
        raise SystemExit("SpaceO 返回了无效的后台状态；未创建虚拟屏")
    if daemon.get("running") is not True:
        raise SystemExit("SpaceO 后台未运行；请先以受管后台运行 agent-spaceo daemon")
    if daemon.get("matchesCLI") is not True:
        raise SystemExit("SpaceO CLI 与后台构建不一致；请先正常更新并重启旧后台")
    if not any(isinstance(item, dict) and item.get("name") == "session-cursors"
               and item.get("available") is True for item in capabilities):
        raise SystemExit("当前 SpaceO 缺少 session-cursors；请安装 agent-screen 配套的 SpaceO 构建")
    return result


def create(args):
    path = state_path(args.session)
    if path.exists():
        raise SystemExit(f"session {args.session} 已存在；请换名称或先 destroy")
    require_single_physical_screen()
    require_compatible_daemon()
    if args.preset == "shared":
        with shared_creation_lock():
            require_shared_pool()
            _create(args, path)
    else:
        _create(args, path)


def _create(args, path):
    require_single_physical_screen()
    command = [
        "session", "create", "--session", args.session, "--preset", args.preset,
        "--controller-ttl", str(args.controller_ttl), "--orphan-grace", "1800", "--json",
    ]
    if args.app:
        command.extend(["--app", args.app])
    if args.title:
        command.extend(["--title", args.title])
    if args.record:
        command.extend(["--record", args.record])
    result = invoke(command)
    response = json_result(result)
    info = response.get("session")
    lease = response.get("controllerLeaseID")
    if not isinstance(info, dict) or not isinstance(lease, str) or not lease:
        raise SystemExit("SpaceO 未返回完整 session/lease；创建结果不能安全使用")
    display_id = info.get("displayID")
    tile = {key: info.get(key) for key in ["x", "y", "width", "height"]}
    if (not isinstance(display_id, int) or display_id <= 0
            or any(not isinstance(value, (int, float)) for value in tile.values())
            or tile["width"] <= 0 or tile["height"] <= 0):
        raise SystemExit("SpaceO 返回了无效的 display/tile")
    try:
        write_state(args.session, lease, display_id, tile)
    except Exception:
        invoke(["session", "destroy", "--session", args.session, "--lease", lease])
        raise
    print(json.dumps({"session": args.session, "display_id": display_id, "tile": tile,
                      "cursor": "spaceo-native"}, ensure_ascii=False, sort_keys=True))


def run_app(args):
    state = heartbeat(args.session)
    command = ["run", args.app, "--session", args.session, "--lease", state["lease"]]
    if args.new_instance:
        command.append("--new-instance")
    if args.timeout:
        command.extend(["--timeout", str(args.timeout)])
    command.extend(args.files)
    return emit(invoke(command))


def adopt(args):
    state = heartbeat(args.session)
    return emit(invoke(["adopt", "--pid", str(args.pid), "--session", args.session,
                        "--lease", state["lease"]]))


def command(args):
    state = heartbeat(args.session)
    if not args.spaceo_args:
        raise SystemExit("command 需要 SpaceO 子命令")
    forbidden = {"create", "claim", "destroy", "stop", "restart", "set", "remove", "trim"}
    if args.spaceo_args[0] in forbidden or "--operator" in args.spaceo_args:
        raise SystemExit("该跨会话或生命周期操作不走通用 command；请使用明确的本地子命令")
    return emit(invoke([*args.spaceo_args, "--session", args.session, "--lease", state["lease"]]))


def heartbeat_command(args):
    heartbeat(args.session)
    print(json.dumps({"session": args.session, "heartbeat": True}, sort_keys=True))


def live_session_ids():
    result = invoke(["session", "list", "--json"])
    if result.returncode:
        return None
    try:
        response = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    sessions = response.get("sessions") if isinstance(response, dict) else None
    if not isinstance(sessions, list):
        return None
    return {session.get("id") for session in sessions if isinstance(session, dict)}


def destroy(args):
    state = load_state(args.session)
    command = ["session", "destroy", "--session", args.session, "--lease", state["lease"]]
    if args.keep_apps:
        command.append("--keep-apps")
    result = invoke(command)
    remaining = None if result.returncode == 0 else live_session_ids()
    if result.returncode == 0 or (remaining is not None and args.session not in remaining):
        remove_state(args.session)
    return emit(result)


def status(args):
    if args.session:
        state = load_state(args.session)
        print(json.dumps({"session": args.session, "display_id": state["display_id"],
                          "tile": state["tile"], "cursor": "spaceo-native"},
                         ensure_ascii=False, sort_keys=True))
        return 0
    result = invoke(["session", "list", "--json"])
    response = json_result(result)
    print(json.dumps(sanitized(response), ensure_ascii=False, sort_keys=True))
    return 0


def daemon(args):
    command = [SPACEO_BIN, "daemon"]
    if args.sessions_per_display:
        command.extend(["--sessions-per-display", str(args.sessions_per_display)])
    if args.display_size:
        command.extend(["--display-size", args.display_size])
    os.execv(command[0], command)


def passthrough(arguments):
    return emit(invoke(arguments))


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="cmd", required=True)
    daemon_parser = sub.add_parser("daemon")
    daemon_parser.add_argument("--sessions-per-display", type=int, default=4)
    daemon_parser.add_argument("--display-size")
    create_parser = sub.add_parser("create")
    create_parser.add_argument("--session", required=True)
    create_parser.add_argument("--preset", default="shared",
                               choices=["shared", "exclusive", "exclusive_1080p", "exclusive_1440p"])
    create_parser.add_argument("--app")
    create_parser.add_argument("--title")
    create_parser.add_argument("--record", choices=["actions", "actions+frames"])
    create_parser.add_argument("--controller-ttl", type=int, default=3600)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--session", required=True)
    run_parser.add_argument("--app", required=True)
    run_parser.add_argument("--new-instance", action="store_true")
    run_parser.add_argument("--timeout", type=int)
    run_parser.add_argument("files", nargs="*")
    adopt_parser = sub.add_parser("adopt")
    adopt_parser.add_argument("--session", required=True)
    adopt_parser.add_argument("--pid", required=True, type=int)
    command_parser = sub.add_parser("command")
    command_parser.add_argument("--session", required=True)
    command_parser.add_argument("spaceo_args", nargs=argparse.REMAINDER)
    heartbeat_parser = sub.add_parser("heartbeat")
    heartbeat_parser.add_argument("--session", required=True)
    destroy_parser = sub.add_parser("destroy")
    destroy_parser.add_argument("--session", required=True)
    destroy_parser.add_argument("--keep-apps", action="store_true")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--session")
    sub.add_parser("doctor")
    sub.add_parser("pool")
    return root


def main():
    args = parser().parse_args()
    if args.cmd == "create":
        if not 30 <= args.controller_ttl <= 3600:
            raise SystemExit("controller-ttl 必须在 30–3600 秒之间")
        create(args)
        return 0
    if args.cmd == "run":
        return run_app(args)
    if args.cmd == "adopt":
        return adopt(args)
    if args.cmd == "command":
        return command(args)
    if args.cmd == "heartbeat":
        heartbeat_command(args)
        return 0
    if args.cmd == "destroy":
        return destroy(args)
    if args.cmd == "status":
        return status(args)
    if args.cmd == "doctor":
        return emit(require_compatible_daemon())
    if args.cmd == "pool":
        return passthrough(["pool"])
    if args.cmd == "daemon":
        daemon(args)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
