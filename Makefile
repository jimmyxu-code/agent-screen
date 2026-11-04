PREFIX ?= $(HOME)/.local
SKILL_HOME ?= $(HOME)/.agents/skills/agent-screen
SWIFTC ?= xcrun swiftc
PYTHON ?= python3
BINARY = .build/agent-screen
INPUT = skills/agent-screen/scripts/agent-input.py
SPACEO_INPUT = skills/agent-screen/scripts/agent-spaceo.py
BROWSER = skills/agent-screen/scripts/agent-browser.py
BROWSER_VIEWER = skills/agent-screen/scripts/agent-browser-viewer.html
VIEWER_HOME = $(PREFIX)/share/agent-browser

.PHONY: build test install clean
build: $(BINARY)

$(BINARY): agent-screen.swift
	mkdir -p .build
	$(SWIFTC) -O -warnings-as-errors -sdk "$$(xcrun --sdk macosx --show-sdk-path)" $< -o $@

test: build
	$(PYTHON) -m unittest discover -s Tests -v

install: test
	$(PYTHON) -c 'import Quartz, ApplicationServices, AppKit'
	install -d "$(PREFIX)/bin" "$(SKILL_HOME)/scripts" "$(VIEWER_HOME)"
	install -m 755 $(BINARY) "$(PREFIX)/bin/agent-screen"
	install -m 755 $(INPUT) "$(PREFIX)/bin/agent-input"
	install -m 755 $(SPACEO_INPUT) "$(PREFIX)/bin/agent-spaceo"
	install -m 755 $(BROWSER) "$(PREFIX)/bin/agent-browser"
	install -m 644 $(BROWSER_VIEWER) "$(VIEWER_HOME)/viewer.html"
	install -m 644 skills/agent-screen/SKILL.md "$(SKILL_HOME)/SKILL.md"
	install -m 755 $(INPUT) "$(SKILL_HOME)/scripts/agent-input.py"
	install -m 755 $(SPACEO_INPUT) "$(SKILL_HOME)/scripts/agent-spaceo.py"
	install -m 755 $(BROWSER) "$(SKILL_HOME)/scripts/agent-browser.py"
	install -m 644 $(BROWSER_VIEWER) "$(SKILL_HOME)/scripts/agent-browser-viewer.html"
	# Overwriting a signed binary in place leaves a stale code signature and macOS kills it.
	codesign --force --sign - "$(PREFIX)/bin/agent-screen"

clean:
	rm -rf .build
