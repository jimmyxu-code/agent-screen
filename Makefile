PREFIX ?= $(HOME)/.local
SWIFTC ?= xcrun swiftc
BINARY = .build/agent-screen

.PHONY: build test install clean
build: $(BINARY)

$(BINARY): agent-screen.swift
	mkdir -p .build
	$(SWIFTC) -O -warnings-as-errors -sdk "$$(xcrun --sdk macosx --show-sdk-path)" $< -o $@

test: build
	python3 -m unittest discover -s Tests -v

install: test
	install -d "$(PREFIX)/bin"
	install -m 755 $(BINARY) "$(PREFIX)/bin/agent-screen"

clean:
	rm -rf .build
