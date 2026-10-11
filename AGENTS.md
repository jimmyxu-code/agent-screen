# Agent work location

- Multiple physical displays: place the agent's work window on the second physical display.
- One physical display: use a SpaceO MCP session.

Call `agent_screen_prepare`, then `agent_screen_status(pid)` to discover the window index,
then `agent_screen_move(pid, window_index)` before driving that window with a computer-use tool.
Place new work windows the same way. CLI: `~/.local/bin/agent-screen`.

For single-display work, read SpaceO pool/doctor first. Create a session only when health allows,
then verify window placement and isolation. Stop SpaceO graphical operations when health is
blocked or unknown. Preserve accurate partial-isolation results.

Repository checks: `make test`, `git diff --check`. Tests must not move windows, synthesize input,
change display topology, or alter privacy permissions. Keep machine-specific configuration,
diagnostic logs, recordings, and credentials outside this repository.
