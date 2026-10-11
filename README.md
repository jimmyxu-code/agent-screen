# agent-screen

[中文说明](README.zh-CN.md)

A small macOS CLI and MCP server for placing agent work windows on a second physical display.
On a single physical display, it reports `mode: spaceo` so the agent can use a separate
[SpaceO](https://github.com/ParthJadhav/SpaceO) MCP session.

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

Configure SpaceO separately for single-display work. This repository provides the routing
signal and physical-window placement; SpaceO owns its virtual-display sessions and input routing.

## Workflow

1. Call `agent_screen_prepare` before opening a work window.
2. If `mode` is `second_physical_screen`, open the work app, call `agent_screen_status` with its
   `pid`, then call `agent_screen_move` with the returned `window_index`. Use your computer-use
   tool to drive that window. Place newly opened work windows the same way.
3. If `mode` is `spaceo`, check SpaceO pool/doctor, create a healthy session, and verify placement
   and isolation before driving an app. Preserve blocked/unknown and partial-isolation results.

Physical displays are ordered with the main display first, then the remaining active displays
by display ID. The target is the second entry. SpaceO's virtual displays are excluded. Mirrored
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

## Development and verification

```sh
make test
git diff --check
```

CI builds with warnings treated as errors and exercises MCP negotiation, argument validation,
notification handling, bounded request framing, and invalid CLI input. These checks do not move
windows or synthesize input. Actual multi-display placement requires a local extended-display
setup and Accessibility permission. A real single-display SpaceO native workflow has been
checked; keyboard/text routing remains partial where macOS does not expose focus evidence.

This tool uses SpaceO's documented display vendor/model identifiers for filtering. SpaceO remains
an independently installed project. Licensed under [MIT](LICENSE).
