# Agent work location

The user-facing rule is `skills/agent-screen/SKILL.md`. Read and follow it for GUI work; do not create client-specific copies.

- Browser work uses a supervised `agent-browser daemon` and isolated `managed_headless` contexts. Never open or adopt the user's browser, profile, cookies or tabs. `agent-browser show` is only for an explicit human request to watch; the local viewer is read-only until the user enables control. Browser input uses DevTools, never global input or the real pointer.
- Call `agent-screen prepare` first. With one physical display, use a distinct session and tile on
  the shared SpaceO virtual display via `agent-spaceo` instead of operating the user's desktop.
  Use a supervised `agent-spaceo daemon`.
- With multiple physical displays, the built-in panel is the agent screen. Use a supervised
  `agent-screen watch` or a standard MCP-hosted engine; never detach into launchd or an ad-hoc daemon.
- Claim an app before creating task windows and release afterwards. Park and restore only the user
  window being borrowed. Pre-existing or unidentified windows belong to the user.
- Keep controller main interfaces and unrelated user windows untouched.
- Coordinate mouse work requires the visible independent cursor and an expected task PID. Never
  warp, hide, borrow, or restore the real pointer, and never fall back to global HID mouse input.
- SpaceO sessions use their native input and per-session cursor. Verify isolation and real UI
  effects, and never treat a posted event or a partial report as full proof.

Repository checks: `make test` and `git diff --check`. Tests must not move real windows, host the
engine, show the cursor, synthesize input, change display topology, or alter privacy permissions.
Keep machine-specific configuration, diagnostic logs, recordings, and credentials outside this
repository. Real window and cursor verification uses disposable fixtures outside tracked files.
