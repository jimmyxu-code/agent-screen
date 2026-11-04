# agent-screen

[中文说明](README.zh-CN.md)

A macOS CLI, shared Agent Skill, and optional MCP server that keeps GUI task windows and a visible independent agent cursor on the built-in second physical screen, or in a shared SpaceO virtual display with per-task sessions when only one physical screen is available. The user's primary display, controller main interface, and real pointer are left alone.

With several physical displays, the agent screen is selected by built-in hardware, not by display numbering or whichever display macOS currently calls main. With one physical display, the shared Skill creates a SpaceO virtual session instead of operating the desktop directly.

## Install

Requires macOS 14 or later, Swift command-line tools, `make`, Python 3, and PyObjC. The input layer uses the Quartz, ApplicationServices, and CoreText packages:

```sh
python3 -m pip install --user pyobjc-framework-Quartz pyobjc-framework-ApplicationServices pyobjc-framework-CoreText
```

Then install everything with one command:

```sh
git clone https://github.com/jimmyxu-code/agent-screen.git
cd agent-screen
make install
```

This installs and ad-hoc signs:

- `~/.local/bin/agent-screen`
- `~/.local/bin/agent-input`
- `~/.local/bin/agent-spaceo`
- `~/.local/bin/agent-browser` and the local viewer resource `~/.local/share/agent-browser/viewer.html`
- `~/.agents/skills/agent-screen/SKILL.md` and its supporting scripts

`PREFIX` changes the binary destination; `SKILL_HOME` changes the Skill destination. `make install` checks the required Python imports instead of silently installing packages.

## One Skill, not per-agent integrations

The repository contains one generic `skills/agent-screen/SKILL.md`. Agents that scan the shared `~/.agents/skills` directory can discover it; other agents can load the same file through their normal Skill mechanism. There is no client-specific behavior or setup matrix in the project.

A Skill supplies instructions, not macOS permission or a persistent process. It needs one authorized backend shared by all agents:

```sh
~/.local/bin/agent-screen watch
```

Run `watch` as a supervised, persistent background task and keep its host alive. Do not detach it with launchd, `nohup`, or `(cmd &)`: Accessibility may no longer follow the helper. Only one engine is needed; additional agents reuse it.

With one physical display, use `agent-spaceo daemon` as the supervised SpaceO backend instead. It owns virtual sessions and their cursors; physical `watch` and `agent-screen cursor click` are not used for that path. Both daemons use the same supervised-process model and optional standard MCP interfaces, not per-agent configuration.

Hosts without supervised background tasks can use the standard MCP interface instead. No agent-specific MCP registration is required when supervised `watch` works. A host that provides neither capability cannot honestly promise creation-time routing.

### Optional MCP configuration

```json
{
  "mcpServers": {
    "agent_screen": {
      "command": "/absolute/home/.local/bin/agent-screen",
      "args": ["mcp"]
    }
  }
}
```

The first `watch` or MCP process with Accessibility acquires `watch.lock` and hosts the engine and cursor. Other MCP connections can forward cursor requests through the same local channel.

## Browser: managed headless sessions

Use `agent-browser` for browser tasks in either display mode. It starts a private, headless Chromium process, not a desktop Chrome window. Each task receives an isolated browser context, page target, viewport and lease; user browser processes, profiles, cookies and tabs are never adopted. Install Google Chrome or set `AGENT_BROWSER_CHROME` to a managed Chromium executable.

```sh
agent-browser doctor
agent-browser daemon                       # supervised persistent background task
agent-browser create --session unique-task --url https://example.com
agent-browser snapshot --session unique-task
agent-browser click --session unique-task --selector 'a'
agent-browser type --session unique-task --selector '#field' --text 'hello' --replace
agent-browser scroll --session unique-task --dy 400
agent-browser tabs --session unique-task
agent-browser screenshot --session unique-task -o browser.png
agent-browser heartbeat --session unique-task
agent-browser destroy --session unique-task
```

All browser input uses DevTools, not global mouse/keyboard events, the real pointer or shared-clipboard shortcuts. Screenshots contain the session's visible blue cursor. `prepare` reports `browser_route=managed_headless` and `browser_command=agent-browser` for this route. A SpaceO health refusal never falls back to the real desktop.

When the user explicitly asks to watch, use `agent-browser show --session unique-task`. The loopback-only live viewer uses a one-time ticket followed by an HttpOnly, SameSite=Strict local cookie. It is read-only by default; only the user may enable its control checkbox. The managed profile starts signed out, so the user can log in through the viewer when needed without sharing a browser session or disclosing passwords to the agent.

A host's embedded browser tool is allowed only when it keeps its panel hidden during background work. Reveal a panel only when the user asks to watch; otherwise use `agent-browser`. Session leases and viewer credentials never appear in command output or logs.

## Workflow

1. Run `agent-screen prepare` (read-only).
2. If `mode` is `single_physical_screen`, require `target_kind=spaceo_virtual` and use the SpaceO workflow below instead of operating the real desktop.
3. If `mode` is `second_physical_screen`, confirm `target_built_in=true` and ensure `engine_running=true`.
4. For an app that will create task windows, run `agent-screen claim PID` before creating them. The engine moves new windows to the built-in screen on creation events and rechecks them every 0.2 seconds. Existing windows on any Space remain the user's; unknown identities are also protected. Run `agent-screen release PID` afterwards to close only windows created during the claim.
5. To borrow an existing user window, use `agent-screen status PID`, `agent-screen park PID INDEX`, then `agent-screen restore PID INDEX`. This visibly moves the existing window, so prefer claim for task-created windows.

Only claim or park the current task's target. Never claim or move the controller's normal main interface or unrelated user windows.

```sh
agent-screen prepare
agent-screen status [PID]
agent-screen claim PID [MINUTES]        agent-screen release PID | --all
agent-screen park PID [INDEX] [MINUTES] agent-screen restore PID [INDEX] | --all
agent-screen sweep PID                  agent-screen watch
```

`agent_screen_move` remains an alias of `agent_screen_park` for compatibility.

## One physical screen: SpaceO

Install the companion SpaceO build containing per-session cursors and automatic-mirror handling, based on SpaceO 1.0.9 (`SpaceO-agent-screen` is the source tree used for this integration). Official upstream 1.0.9 alone does not include those changes. Use `agent-spaceo doctor` to require a usable host, a matching running daemon and the `session-cursors` capability; its report is JSON. `create` performs the same check before pool access or display allocation, rejecting stale daemons and builds without the companion capability. Bounded display identities prevent the random-identity ColorSync growth in older builds. Session creation and cursor management stay inside the SpaceO daemon, not in agent-screen.

```sh
agent-spaceo doctor
agent-spaceo daemon                       # supervised persistent background task
agent-spaceo create --session unique-task
agent-spaceo run --session unique-task --app Application
agent-spaceo command --session unique-task ax
agent-spaceo command --session unique-task click --element 3
agent-spaceo command --session unique-task screenshot -o session.png
agent-spaceo command --session unique-task verify
agent-spaceo heartbeat --session unique-task
agent-spaceo destroy --session unique-task
```

The supervised daemon divides one virtual display into four task tiles, and `create` defaults to `shared`. A per-user creation lock makes the occupancy check and session creation one step for all agents using this wrapper; a full shared display is refused rather than silently attaching another display. Creation rechecks the physical-screen route: when the built-in second physical screen is available, every virtual-display preset, including exclusive, is refused in favor of the physical-screen workflow above. An unavailable or inconsistent route also refuses creation. Explicit exclusive presets remain available on one physical screen for separate operator-controlled use cases.

Inspect a session with `agent-spaceo command --session unique-task screenshot -o session.png`, or open the live Viewer with `open -g -a "SpaceO Viewer" --args --background`. The Viewer is a window onto the virtual display; with an external display it follows the physical-screen rules on the built-in panel. Before coordinate operations on the Viewer, claim its newly launched PID with `agent-screen claim PID`; never move the controller's main interface.

Each session and tile has its own visible blue cursor, owned by the SpaceO daemon and included in session captures. `agent-spaceo` stores leases only in owner-only local state and never prints them. Use `run` for task-created apps. Use `adopt --pid` only when the user explicitly asks to borrow an existing app, and finish that work with `destroy --keep-apps` to release it. Never adopt a controller main interface.

Use fresh indices and verify actual UI/document effects. A `partial` isolation report is not a complete pass; `breached` means stop. Finish with `destroy` for task-created apps or `--keep-apps` for borrowed apps, then check `agent-spaceo status` and `agent-spaceo pool` for leftovers without disturbing other sessions. Normal work never sets `SPACEO_LIVE_TESTS` or `SPACEO_TESTING_HOST`; those flags are reserved for diagnostic runs.

## Multiple physical screens: independent visible cursor

`watch` displays a blue, non-activating, click-through arrow on the built-in screen. It is an engine-owned panel, not the WindowServer's single hardware cursor, and remains visible after individual CLI commands exit.

```sh
agent-screen cursor status
agent-screen cursor move X Y
agent-screen cursor click X Y --pid PID [--double] [--shift]
agent-input click X Y --pid PID [--double] [--shift]
```

Require `visible=true` and `target_built_in=true` before coordinate work. Clicks are rejected outside the built-in screen or when the top window at the point belongs to a different PID. Mouse events go directly to the target process with its window number and a window-local location. The implementation never warps, hides, borrows, or restores the real pointer and never falls back to global HID mouse events.

A `posted` result means events were sent, not that the app performed the action. Verify the real UI or document state after clicking. Native AppKit buttons and custom AppKit views have been exercised; other UI frameworks need their own verification.

The process-direct click relies on exported but undocumented CoreGraphics window fields and a window-location setter absent from public headers. These are compatibility-sensitive interfaces and should be regression-tested after macOS updates. They do not use a private driver or the global event stream.

Prefer AX menu/control actions and directed keyboard input. If activation is needed, activate and verify the app first, then re-read the current coordinates before calling either click command; neither command activates the app or updates stale coordinates for you. Global keyboard input is a separate, explicitly confirmed operation, never a mouse fallback.

## Engine state and boundaries

Claims are enforced only while an authorized engine is running. `agent_screen_status` reads its heartbeat; `claim` warns instead of pretending enforcement when it is absent. Leases default to 30 minutes (maximum 1440). Expired claims close their agent windows and expired parks restore themselves. `status` also settles expired parks when no engine is running.

Ledgers live in `~/.local/state/agent-screen/` with mode 0600 and locked read-modify-write. Logs live in `~/Library/Logs/AgentScreen/engine.log`.

Only active physical screens affect routing; known virtual displays are excluded. Mirrored setups are refused. Minimized windows must be restored before parking. Full-screen windows leave full screen while parked and re-enter it on their original display. The tool does not rearrange displays or change privacy settings.

## Development and verification

```sh
make test
git diff --check
```

Tests set `AGENT_SCREEN_ENGINE=off`, so they do not host the engine, show the cursor, move real windows, or synthesize input. They cover MCP negotiation, argument validation, notification handling, bounded framing, route descriptions, shared-Skill packaging, the ban on real-pointer mouse operations, and SpaceO lifecycle with a fake CLI. Real window and cursor behavior needs local validation with extended physical or SpaceO virtual displays and Accessibility permission.

Licensed under [MIT](LICENSE).
