# Agent work location

- Multiple physical displays: place the agent's work window on the second physical display.
- Only move specific work windows opened for the current task or explicitly designated by the user.
  Keep controller main interfaces and other existing user windows on the user's primary display.
- One physical display: operate task windows directly on that display using computer-use tools.
  No SpaceO session, extra isolation, health check, or screen-routing permission confirmation is needed.

For multiple physical displays, call `agent_screen_prepare`, then `agent_screen_status(pid)` to discover the window index,
then `agent_screen_move(pid, window_index)` before driving that window with a computer-use tool.
Place new work windows the same way. CLI: `~/.local/bin/agent-screen`.

Repository checks: `make test`, `git diff --check`. Tests must not move windows, synthesize input,
change display topology, or alter privacy permissions. Keep machine-specific configuration,
diagnostic logs, recordings, and credentials outside this repository.
