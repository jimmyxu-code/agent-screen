---
name: agent-screen
description: Isolate browser automation in managed headless Chromium with an on-demand live viewer, and route other macOS GUI work to the built-in second physical screen or a SpaceO virtual screen while preserving the user's display and real pointer. Use for any browser or macOS GUI task.
---

# Agent Screen

Never claim, park, adopt, or bulk-move the controller's normal main interface or unrelated user windows. Never change display arrangement or privacy settings as part of a GUI task.

Default commands are `~/.local/bin/agent-screen`, `~/.local/bin/agent-input`, `~/.local/bin/agent-spaceo`, and `~/.local/bin/agent-browser`. `AGENT_SCREEN_BIN` overrides the first; `SPACEO_BIN` overrides SpaceO. `AGENT_BROWSER_CHROME` may select a managed Chromium executable, never an existing browser process.

## 1. Read the route first

Run `~/.local/bin/agent-screen prepare` before GUI work. It is read-only and moves neither windows nor either pointer.

- Browser work: require `browser_route=managed_headless` and `browser_command=agent-browser`; use section 2 in either display mode. It creates no desktop browser window and does not depend on SpaceO admission.
- `second_physical_screen`: require `target_kind=built_in` and `target_built_in=true`; use sections 3–5 for other GUI work.
- `single_physical_screen`: require `virtual_screen_required=true` and `target_kind=spaceo_virtual`; use section 6 for other GUI work. Never operate the user's desktop directly.

## 2. Browser: managed headless sessions and on-demand viewing

Use a separate managed Chromium process, never the user's browser, profile, cookies, tabs, or existing DevTools endpoint. A daemon lazily starts one private headless Chrome; every task gets an isolated browser context, target, viewport and lease. Browser contexts do not share storage or login state.

1. Run `agent-browser doctor`. If no daemon is running, start `agent-browser daemon` as a supervised persistent background task and check again. Reuse that daemon across agents; no per-agent MCP registration is needed. Do not detach it with launchd, `nohup`, or `(cmd &)`.
2. Create a distinct session:
   `agent-browser create --session <unique-task-name> --url <https-url>`
   The default viewport is 1280×900; `--width` and `--height` may change it. `create` never opens a desktop browser window. Leases are stored only in owner-only local state and are never printed.
3. Operate through the managed session:
   - `snapshot` reads current text and interactive elements. `navigate` opens only HTTP(S) URLs or `about:blank`.
   - `click --selector <css>` or `click --text <text>` uses the current DOM. `click --x <x> --y <y>` uses CSS viewport coordinates for canvas content.
   - `type --selector <css> --text <text> [--replace]`, `key`, `scroll`, and `move` go through DevTools. `evaluate` is available for task-specific DOM work.
   - `tabs`, `new-tab`, and `activate-tab` change only the managed target, never the user's foreground. Handle JavaScript dialogs with `dialog`; validate local files with `upload`; inspect task-private downloads with `downloads`.
   - `screenshot -o <path>` returns the real page image with the session's visible blue cursor. `heartbeat` keeps long tasks alive.
   Re-read after DOM changes and verify actual UI effects. A posted event is not proof of an effect. Never use global mouse/keyboard input, real-pointer movement, or shared-clipboard shortcuts. Refused external protocols or unsupported native dialogs must not fall back to the desktop.
4. When the user explicitly asks to watch, run `agent-browser show --session <id>`. It opens a local live viewer through a one-time ticket followed by an HttpOnly, SameSite=Strict local cookie. The viewer is read-only by default. Only the user may enable its control checkbox to forward clicks, text and scrolling to the managed page. The agent must not enable control for the user or use the user's normal browser as the task browser.
   The managed profile starts signed out. If login is needed, let the user log in through the viewer; never borrow the user's existing browser session or request passwords through the page or chat.
5. Finish with `agent-browser destroy --session <id>`. Confirm `agent-browser doctor` has no task session, browser process or local lease left behind. Keep other agents' sessions and the shared daemon intact.

A host's embedded browser tool may be used only when it can keep its panel hidden and preserve the user's foreground. Never activate or reveal its panel for background work; only reveal it when the user explicitly asks to watch. Otherwise use `agent-browser`.

## 3. Multiple physical screens: shared backend

If `engine_running` is false, run `~/.local/bin/agent-screen watch` as a supervised, persistent background task, then check `agent-screen status`. Keep the host alive; do not detach with launchd, `nohup`, or `(cmd &)`, because Accessibility may no longer follow the helper.

Reuse one authorized backend across agents. A standard `agent-screen mcp` connection may also host it, but no agent-specific MCP registration is required when supervised `watch` works. If no authorized persistent backend can run, stop before creating task windows and report the limitation; a Skill alone does not supply permission or enforcement.

## 4. Multiple physical screens: task windows

For an application in which the task will create windows:

1. `agent-screen claim PID [MINUTES]` before activating the app or creating any window.
2. Open and operate task windows normally.
3. `agent-screen release PID` when finished, closing windows created during that claim.

Existing or unidentified windows belong to the user and must remain untouched. For an existing user window that must be borrowed:

1. `agent-screen status PID` and select the correct index.
2. `agent-screen park PID INDEX [MINUTES]`.
3. `agent-screen restore PID INDEX` immediately after use.

A park/restore pair visibly moves the existing window, so prefer claim for task-created windows. Do not use park to avoid starting the backend for new task windows.

## 5. Multiple physical screens: independent cursor

Before coordinate mouse work, run `agent-screen cursor status` and require `visible=true` and `target_built_in=true`. The visible arrow is independent of the user's real pointer and remains on the built-in screen.

Prefer AX menu/control actions and directed keyboard input. For coordinate clicks:

1. Make the intended task window the visible top window at that point. If activation is needed, activate and verify the app first, then re-read current coordinates; never reuse pre-activation coordinates.
2. Use `agent-input click X Y --pid PID [--double] [--shift]`, or equivalent `agent-screen cursor click`. Neither command activates the app or changes coordinates for you.
3. Read the UI or document state afterwards. A `posted` result means events were delivered, not that the app performed the action.

Clicks are rejected outside the built-in screen or when the top window belongs to another PID. Never fall back to pointer tools that warp, hide, borrow, or restore the real pointer, and never use global HID mouse events for this work. Global keyboard input remains allowed only after confirming the intended app is frontmost.

## 6. One physical screen: SpaceO virtual sessions

Use SpaceO's own session, isolation, input, capture, and per-session cursor management. Do not use physical-screen claim/park or `agent-screen cursor click` for this path.

1. Run `agent-spaceo doctor` and require a usable host, a matching daemon build, and the `session-cursors` capability. The companion SpaceO build provides this capability; an upstream build with the same version number is not sufficient. If no daemon is running, start `agent-spaceo daemon` as a supervised, persistent background task and check again. Reuse the shared daemon instead of starting another. A standard SpaceO MCP connection is optional for hosts without supervised background tasks; no per-agent registration is needed otherwise. If the host is blocked, stop and report it instead of using the real desktop or deleting safety evidence.
2. Create a distinct session and tile for this agent/task on the shared virtual display:
   `agent-spaceo create --session <unique-task-name>`
   The daemon started by this Skill divides one virtual display into four task tiles; creation defaults to `shared`, not a new display per agent. Other agents create different sessions and tiles, never another agent's session or lease. `create` rechecks the physical-screen route and refuses every virtual-display preset when a built-in second physical screen is available; use sections 3–5 instead. It also refuses when the route cannot be confirmed. The result contains only the session/display/tile and native-cursor route. `agent-spaceo` stores leases only in owner-only local state, never in its output.
3. For a task-created app, launch with `agent-spaceo run --session <id> --app <application>`. SpaceO places windows and creates the session cursor. Use `--new-instance` only when task isolation requires it. SpaceO's supported native-app and managed-Chromium boundaries still apply; managed Electron is refused, not silently routed elsewhere.
4. To operate an existing user app, use `agent-spaceo adopt --session <id> --pid <pid>` only when the user explicitly asked to borrow that app. It adopts the app's windows into the session; never adopt a controller main interface. End borrowed work with `agent-spaceo destroy --session <id> --keep-apps` to release rather than terminate it.
5. Operate and verify through the same session:
   - `agent-spaceo command --session <id> ax` for indexed elements.
   - `agent-spaceo command --session <id> click --element <N>`, `type`, `key`, `scroll`, `move`, or `drag` for actions.
   - `agent-spaceo command --session <id> screenshot -o <path>` to inspect the session and its visible cursor position.
   - `agent-spaceo command --session <id> verify` for isolation evidence.
   - `agent-spaceo heartbeat --session <id>` during long reasoning intervals.
   Use fresh indices after every UI change and verify the real UI/document effect, not just a posted action. If verification is `partial`, report the uncovered checks rather than calling it a complete isolation pass. If it is `breached`, stop.
6. Finish with `agent-spaceo destroy --session <id>` for task-created apps, or `--keep-apps` for borrowed apps. Confirm no session/display or cursor from this task remains, without disturbing other agents' sessions.

The session cursor is a distinct visible arrow for every session and tile, managed by the SpaceO daemon and included in session captures. It never moves the user's real pointer. Do not add `SPACEO_LIVE_TESTS` or `SPACEO_TESTING_HOST` to normal work; those are diagnostic-only flags, not the production route.

## 7. Finish and verify

For browser work, confirm `agent-browser doctor` has no task session, unmanaged browser process or local lease. For physical multi-screen work, confirm `agent-screen status` has no task claim or parked window. For single-screen SpaceO work, confirm `agent-spaceo status` and `agent-spaceo pool` have no task session, display, or cursor left behind. Leave other users' and agents' windows and sessions alone.
