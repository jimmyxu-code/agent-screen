# agent-screen

[中文说明](README.zh-CN.md)

A small macOS CLI and MCP server for placing agent work windows on a second physical display.
On a single physical display, it reports `mode: single_physical_screen`: operate task windows
directly on that screen with normal computer-use tools. SpaceO is not required.

## Install

Requires macOS 14 or later, Swift command-line tools, `make`, and Python 3 for checks.
The current workflow has been exercised on Apple Silicon. Other Mac configurations need their
own window-placement validation.

```sh
git clone https://github.com/jimmyxu-code/agent-screen.git
cd agent-screen
make install
```

The executable is installed at `~/.local/bin/agent-screen`. Use `make install PREFIX=/your/prefix`
to select another destination. Grant Accessibility access to the host app that launches the tool
in System Settings → Privacy & Security → Accessibility.

## MCP configuration

Add this entry to your client's MCP configuration, expanding the home directory to an absolute
path supported by your client:

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

The tool reports the physical-screen route and places individual task windows. Your existing
computer-use tools provide input on both single-display and multi-display setups.

## Workflow

1. Call `agent_screen_prepare` before opening a work window.
2. If `mode` is `second_physical_screen`, open the work app, call `agent_screen_status` with its
   `pid`, then call `agent_screen_move` with the returned `window_index`. Use your computer-use
   tool to drive that window. Place newly opened work windows the same way.
3. If `mode` is `single_physical_screen`, operate directly on that display. There is no required
   placement step, SpaceO session, extra isolation, or health check.

Only move specific task windows you opened or the user explicitly designated. Keep the normal
WorkBuddy, Codex, DeepSeek Harness, and other controller interfaces on the user's primary screen.
Never move all of an application's windows together.

Physical displays are ordered with the main display first, then the remaining active displays
by display ID. The target is the second entry when multiple physical displays are connected, otherwise the sole display. SpaceO's virtual displays are excluded. Mirrored
setups are refused; use extended displays. Full-screen or minimized work windows must be restored
before moving them. Window placement is confirmed from the resulting bounds.

The tool moves a specified window through Accessibility. Computer-use tools supply input.
Pointer travel between displays follows macOS display arrangement; arrange the external display
left of the built-in display to cross from its right edge into the built-in display.

```sh
agent-screen prepare
agent-screen status PID
agent-screen move PID WINDOW_INDEX
```

## Window visibility

If a computer-use tool can read or capture a task window but coordinate clicks report
`noWindowsAvailable`, check whether that window is on the current macOS desktop. A controller
in full-screen mode can occupy a separate desktop from the task app. Returning to the chat can
switch back to that full-screen desktop, so manually focusing the task app before replying does
not establish that its window remains visible. Use a normal controller window on the same
desktop for coordinate-based work, or use the computer-use tool's supported accessibility actions.
This does not require a SpaceO session or changes to privacy permissions.

## Development and verification

```sh
make test
git diff --check
```

CI builds with warnings treated as errors and exercises MCP negotiation, argument validation,
notification handling, bounded request framing, invalid CLI input, tool routing instructions,
and a read-only physical-screen route check. These checks do not move windows or synthesize
input. Actual window operation requires local validation with the chosen computer-use tool;
multi-display placement also requires an extended-display setup and Accessibility permission.

This tool uses SpaceO's documented display vendor/model identifiers for filtering. SpaceO remains
an independently installed project. Licensed under [MIT](LICENSE).
