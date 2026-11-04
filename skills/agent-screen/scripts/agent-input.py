#!/usr/bin/env python3
"""agent-input —— 不借用用户真实指针的 GUI 输入层。

窗口和独立光标由同一个 agent-screen 后台负责。多屏时先 claim / park，并确认
agent-screen cursor status 中 visible=true、target_built_in=true。

按打扰程度从低到高选用，能用上一级就不用下一级：
1. menu      通过 AX 触发菜单项：不动指针、不发键盘、不切焦点
2. ax-press  直接按下可访问控件（原生 AppKit 面板、对话框按钮都可用）
3. key       定向键盘（CGEventPostToPid）：只进目标进程，不抢系统焦点；部分自绘应用不接收
4. hkey      全局键盘：发给当前前台应用。调用前必须确认目标在前台（front <pid>）
5. click     第二屏独立可见光标：直接投递给目标进程，不移动、隐藏或借用真实指针。
             无后台时失败，不回退到真实指针。需要置前时先置前并重读坐标；点击后核对 UI 效果。
   ax-select 基于 click 的列表选择（--to 为 Shift 范围选）

用法：
  agent-input menu      <pid> <菜单标题> <菜单项标题>
  agent-input ax-press  <pid> <标题或值>
  agent-input ax-select <pid> <文本> [--to <文本>]
  agent-input ax-center <pid> <标题或值>      打印元素中心坐标
  agent-input ax-dump   <pid> [--depth N]     导出可访问性树
  agent-input key       <pid> <combo>         例: cmd+4 / cmd+shift+g / enter / esc / tab
  agent-input hkey      <combo>
  agent-input click     <x> <y> --pid <pid> [--double] [--shift]
  agent-input front     [<pid>]               切换到目标应用并确认；不带 pid 时只查询
  agent-input probe                           打印独立光标与用户真实指针的位置

坐标约定：全部使用全局逻辑坐标（point，左上角为原点，与 AX 一致）。
Retina 截图的物理像素需除以 2 再加上该屏原点。
"""

import ctypes
import json
import os
import subprocess
import sys
import time

import Quartz
import ApplicationServices as AS
from AppKit import NSWorkspace, NSRunningApplication, NSApplicationActivateIgnoringOtherApps

AS.AXUIElementSetMessagingTimeout(AS.AXUIElementCreateSystemWide(), 3)
AGENT_SCREEN = os.environ.get("AGENT_SCREEN_BIN", os.path.expanduser("~/.local/bin/agent-screen"))

KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9,
    "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19,
    "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26, "8": 28, "0": 29, "o": 31,
    "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46,
    "return": 36, "enter": 76, "tab": 48, "space": 49, "esc": 53, "escape": 53,
    "delete": 51, "backspace": 51, "left": 123, "right": 124, "down": 125, "up": 126,
    ",": 43, ".": 47, "/": 44, "-": 27, "=": 24, "[": 33, "]": 30,
}
MODS = {
    "cmd": Quartz.kCGEventFlagMaskCommand,
    "command": Quartz.kCGEventFlagMaskCommand,
    "shift": Quartz.kCGEventFlagMaskShift,
    "opt": Quartz.kCGEventFlagMaskAlternate,
    "alt": Quartz.kCGEventFlagMaskAlternate,
    "ctrl": Quartz.kCGEventFlagMaskControl,
    "control": Quartz.kCGEventFlagMaskControl,
    "fn": Quartz.kCGEventFlagMaskSecondaryFn,
}


def parse_combo(combo):
    flags = 0
    key = None
    for part in (p.strip().lower() for p in combo.split("+")):
        if not part:
            continue
        if part in MODS:
            flags |= MODS[part]
        else:
            key = part
    if key is None:
        raise SystemExit(f"组合键缺少主键: {combo}")
    if key not in KEYCODES:
        raise SystemExit(f"未知按键: {key}")
    return KEYCODES[key], flags


# ---------- Accessibility ----------

def ax_get(el, attr):
    err, val = AS.AXUIElementCopyAttributeValue(el, attr, None)
    return val if err == 0 else None


def ax_app(pid):
    app = AS.AXUIElementCreateApplication(int(pid))
    AS.AXUIElementSetMessagingTimeout(app, 6)
    return app


def ax_windows(pid):
    """应用的窗口，按前后顺序（第一个是最前面的窗口）。"""
    return ax_get(ax_app(pid), "AXWindows") or []


def ax_walk(root, max_nodes=6000):
    out = []
    queue = [root]
    while queue and len(out) < max_nodes:
        el = queue.pop(0)
        out.append(el)
        queue.extend(ax_get(el, "AXChildren") or [])
    return out


def element_frame(el):
    """返回元素的 (x, y, width, height) 逻辑坐标，不可用时返回 None。"""
    pos = ax_get(el, "AXPosition")
    size = ax_get(el, "AXSize")
    if pos is None or size is None:
        return None
    point = (ctypes.c_double * 2)()
    extent = (ctypes.c_double * 2)()
    if not AS.AXValueGetValue(pos, AS.kAXValueCGPointType, point):
        return None
    if not AS.AXValueGetValue(size, AS.kAXValueCGSizeType, extent):
        return None
    return point[0], point[1], extent[0], extent[1]


def element_center(el):
    """返回元素中心的 (x, y) 逻辑坐标，不可用时返回 None。"""
    frame = element_frame(el)
    if frame is None:
        return None
    x, y, w, h = frame
    return x + w / 2.0, y + h / 2.0


def require_center(el, what):
    center = element_center(el)
    if center is None:
        raise SystemExit(f"无法取得{what}的坐标")
    return center


def ax_find_by_text(pid, text):
    for window in ax_windows(pid):
        for el in ax_walk(window):
            if ax_get(el, "AXTitle") == text or ax_get(el, "AXValue") == text:
                return el
    return None


def ax_press_element(el):
    return AS.AXUIElementPerformAction(el, "AXPress") == 0


def ax_press(pid, title):
    el = ax_find_by_text(pid, title)
    if el is None:
        raise SystemExit(f"未找到元素: {title}")
    return ax_press_element(el)


def menu_press(pid, menu_title, item_title):
    """通过 AX 触发菜单项：零指针、零键盘。"""
    bar = ax_get(ax_app(pid), "AXMenuBar")
    if bar is None:
        raise SystemExit("应用没有可访问的菜单栏")
    for bar_item in ax_get(bar, "AXChildren") or []:
        if ax_get(bar_item, "AXTitle") != menu_title:
            continue
        for menu in ax_get(bar_item, "AXChildren") or []:
            for item in ax_get(menu, "AXChildren") or []:
                if ax_get(item, "AXTitle") == item_title:
                    return ax_press_element(item)
    raise SystemExit(f"未找到菜单项: {menu_title} → {item_title}")


def ax_dump(pid, depth=10):
    lines = []

    def walk(el, d):
        role = ax_get(el, "AXRole")
        title = ax_get(el, "AXTitle")
        value = ax_get(el, "AXValue")
        desc = ax_get(el, "AXDescription")
        selected = ax_get(el, "AXSelected") is True
        lines.append("  " * d + f"{role} t={title!r} v={value!r} d={desc!r}" + (" SELECTED" if selected else ""))
        if d >= depth:
            return
        for child in (ax_get(el, "AXChildren") or [])[:40]:
            walk(child, d + 1)

    for window in ax_windows(pid):
        lines.append(f"=== WINDOW {ax_get(window, 'AXTitle')!r} sub={ax_get(window, 'AXSubrole')!r}")
        walk(window, 0)
    print("\n".join(lines))


# ---------- Applications ----------

def front_pid():
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.processIdentifier() if app else None


def activate(pid, tries=14, settle=0.25):
    """把目标应用切到前台，并确认它确实成了前台应用。"""
    pid = int(pid)
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        raise SystemExit(f"进程 {pid} 不存在")
    if app.isHidden():
        app.unhide()
    for _ in range(tries):
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        time.sleep(settle)
        if front_pid() == pid:
            return True
        AS.AXUIElementSetAttributeValue(ax_app(pid), "AXFrontmost", True)
        time.sleep(0.2)
        if front_pid() == pid:
            return True
    return False


def require_front(pid):
    if not activate(pid):
        raise SystemExit(f"无法把进程 {pid} 切到前台，停止输入以免发给错误的应用")


# ---------- Pointer and keyboard ----------

def user_pointer():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def cursor(action, x=None, y=None, pid=None, double=False, shift=False):
    command = [AGENT_SCREEN, "cursor", action]
    if x is not None:
        command.extend([str(x), str(y)])
    if action == "click":
        if pid is None:
            raise SystemExit("click 需要指定任务 --pid")
        command.extend(["--pid", str(int(pid))])
    if double:
        command.append("--double")
    if shift:
        command.append("--shift")
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=8)
    except FileNotFoundError:
        raise SystemExit("找不到 agent-screen；请先完成安装")
    if result.returncode:
        raise SystemExit(result.stderr.strip() or "独立光标操作失败")
    return json.loads(result.stdout)


def click(pid, x, y, double=False, shift=False):
    """使用第二屏独立可见光标定向点击指定任务，不接触真实指针。"""
    return cursor("click", x, y, pid=pid, double=double, shift=shift)


def _post_key(combo, post, delay):
    code, flags = parse_combo(combo)
    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if flags:
            Quartz.CGEventSetFlags(event, flags)
        post(event)
        time.sleep(delay)


def key_to(pid, combo, delay=0.07):
    _post_key(combo, lambda event: Quartz.CGEventPostToPid(int(pid), event), delay)


def key_global(combo, delay=0.08):
    _post_key(combo, lambda event: Quartz.CGEventPost(Quartz.kCGHIDEventTap, event), delay)


def ax_select(pid, text, to_text=None):
    """选中列表项；给 to_text 时用 Shift 点击做范围选择。返回选中数量。"""
    first = ax_find_by_text(pid, text)
    if first is None:
        raise SystemExit(f"未找到列表项: {text}")
    click(pid, *require_center(first, f"列表项 {text}"))
    if to_text:
        last = ax_find_by_text(pid, to_text)
        if last is None:
            raise SystemExit(f"未找到列表项: {to_text}")
        click(pid, *require_center(last, f"列表项 {to_text}"), shift=True)
    return sum(1 for window in ax_windows(pid) for el in ax_walk(window) if ax_get(el, "AXSelected") is True)


# ---------- CLI ----------

def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    cmd, args = argv[1], argv[2:]

    def need(count):
        if len(args) < count:
            raise SystemExit(__doc__)

    def option(name):
        if name not in args:
            return None
        i = args.index(name)
        if i + 1 >= len(args):
            raise SystemExit(f"{name} 需要一个值")
        return args[i + 1]

    if cmd == "probe":
        print(json.dumps({"agent_cursor": cursor("status"), "user_pointer": user_pointer()}, ensure_ascii=False))
    elif cmd == "front":
        if args:
            print("activated:", activate(args[0]))
        print("front_pid:", front_pid())
    elif cmd == "menu":
        need(3)
        print("ok" if menu_press(args[0], args[1], args[2]) else "failed")
    elif cmd == "key":
        need(2)
        key_to(args[0], args[1])
        print("ok")
    elif cmd == "hkey":
        need(1)
        key_global(args[0])
        print("ok")
    elif cmd == "click":
        need(4)
        pid = None
        flags = set()
        index = 2
        while index < len(args):
            flag = args[index]
            if flag == "--pid" and pid is None and index + 1 < len(args):
                pid = args[index + 1]
                index += 2
            elif flag in {"--double", "--shift"} and flag not in flags:
                flags.add(flag)
                index += 1
            else:
                raise SystemExit(f"不支持的 click 参数: {flag}")
        print(json.dumps(click(pid, args[0], args[1], double="--double" in flags,
                               shift="--shift" in flags), ensure_ascii=False))
    elif cmd == "ax-dump":
        need(1)
        print_depth = option("--depth")
        ax_dump(args[0], int(print_depth) if print_depth else 10)
    elif cmd == "ax-press":
        need(2)
        print("ok" if ax_press(args[0], args[1]) else "failed")
    elif cmd == "ax-select":
        need(2)
        print("selected:", ax_select(args[0], args[1], option("--to")))
    elif cmd == "ax-center":
        need(1)
        el = ax_find_by_text(args[0], args[1])
        print(element_center(el) if el is not None else None)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
