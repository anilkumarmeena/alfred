# JARVIS: common tasks. Run `make` for the list.

SHELL := /bin/bash
.DEFAULT_GOAL := help
unexport VIRTUAL_ENV  # stops uv warning about a shell-wide venv

# uv's usual homes (its installer, cargo, `pip install --user uv`), appended to PATH: make then finds uv when the
# shell's PATH lacks them, and wrappers ahead of it on PATH (e.g. PMG's ~/.pmg/bin/uv) find the real one.
# make install and make setup fetch the pinned uv into ~/.local/bin when no working uv is found.
empty     :=
UV_DIRS   := $(HOME)/.local/bin $(HOME)/.cargo/bin $(wildcard $(HOME)/Library/Python/*/bin)
export PATH := $(PATH):$(subst $(empty) $(empty),:,$(strip $(UV_DIRS)))
UV_VERSION := 0.13.0
# macOS's make 3.81 runs a plain recipe line (`uv sync`) itself, looking it up on the PATH make started with, not the
# one above. So recipes name uv by its full path, found when each one runs (after uv-bin may have just installed it).
uv         = $(shell PATH="$(PATH)" command -v uv || echo uv)
UV         = $(uv) run
UNAME     := $(shell uname -s)
# Behind a company proxy the browser follows the macOS settings, but uv, curl, go and npm only read HTTPS_PROXY.
# The downloading targets take the proxy macOS uses for GitHub (a set proxy, a PAC file or WPAD), unless one is set.
DOWNLOADS := install uv-bin livekit-bin models models-int8 web-install web-build
ifeq ($(UNAME)$(HTTPS_PROXY)$(https_proxy),Darwin)
ifneq ($(filter $(DOWNLOADS),$(MAKECMDGOALS)),)
SYSTEM_PROXY := $(shell osascript -l JavaScript "$(CURDIR)/deploy/macos-proxy.js" https://github.com 2>/dev/null)
ifneq ($(SYSTEM_PROXY),)
$(info Using the macOS proxy $(SYSTEM_PROXY) (set HTTPS_PROXY to override))
export HTTPS_PROXY := $(SYSTEM_PROXY)
export https_proxy := $(SYSTEM_PROXY)
export HTTP_PROXY  := $(SYSTEM_PROXY)
export http_proxy  := $(SYSTEM_PROXY)
export NO_PROXY    := localhost,127.0.0.1,::1
export no_proxy    := $(NO_PROXY)
# Proxies that inspect TLS sign with a company certificate, which macOS trusts but uv's own store doesn't
export UV_SYSTEM_CERTS := 1
endif
endif
endif
# Main checkout, even when run from a task's worktree (data/worktrees/jarvis-N)
# Paths may contain spaces ("Office Work"): make functions split words on them, so quote paths in recipes
GIT_COMMON := $(shell git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)
MAIN_DIR  := $(or $(shell git rev-parse --path-format=absolute --git-common-dir 2>/dev/null | sed 's|/\.git$$||'),$(CURDIR))
# Main checkout's Python (services, artifact-check), also from a worktree. Override with JARVIS_PYTHON=...
ifeq ($(GIT_COMMON),$(shell git rev-parse --path-format=absolute --git-dir 2>/dev/null))
JARVIS_PYTHON ?= $(UV) python
else
JARVIS_PYTHON ?= "$(MAIN_DIR)/.venv/bin/python"
endif
# Service labels (com.jarvis.* or JARVIS_SERVICE_PREFIX): launchd on macOS, systemd user units on Linux; evaluated
# lazily. Service control always runs in the main
# checkout so a worktree never installs its own copy.
SERVICE_CTL = cd "$(MAIN_DIR)" && $(JARVIS_PYTHON) -m jarvis.setup_local
SERVICES    = $(shell $(SERVICE_CTL) labels)
# Claude login: the user's own, unless .env (or the environment) sets JARVIS_CLAUDE_CONFIG_DIR, a login of JARVIS's own
CLAUDE_DIR := $(or $(JARVIS_CLAUDE_CONFIG_DIR),$(shell sed -n "s/^JARVIS_CLAUDE_CONFIG_DIR=[\"']*\([^\"']*\)[\"']*$$/\1/p" \
               "$(MAIN_DIR)/.env" 2>/dev/null | tail -1))
# ...relative to the main checkout, as jarvis.core.config reads it
CLAUDE_PATH = $(if $(CLAUDE_DIR),$(if $(filter /%,$(CLAUDE_DIR)),,$(MAIN_DIR)/)$(CLAUDE_DIR))
# Kokoro voice: kokoro-onnx's model files, from a pinned Hugging Face mirror (byte-identical to the GitHub release,
# which serves ~120 KB/s per connection), hash-checked
KOKORO_HF   := https://huggingface.co/fastrtc/kokoro-onnx/resolve/8d07950c9b6c87ce6809e9bba7bd494336217c2a
TTS         := models/kokoro-v1.0.onnx
TTS_SHA     := 7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5
VOICES      := models/voices-v1.0.bin
VOICES_SHA  := bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d
# Optional 8-bit Kokoro (make models-int8): only on the GitHub release, so slow
KOKORO_INT8 := https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.int8.onnx
# Noise filter: DeepFilterNet 3 streaming ONNX (torchDF export), pinned and hash-checked
DFN       := models/deepfilternet3-streaming.onnx
DFN_URL   := https://github.com/yuyun2000/SpeechDenoiser/raw/9a1f069f562089c2f56008a8ecd897fa981fbc50/48k/denoiser_model.onnx
DFN_SHA   := fe5eb64fa2e4154c83f8e4935e82871c850c154387ee892e0ab65fe179e7d8c9
# Speaker check: WeSpeaker ResNet34, pinned and hash-checked; keep in step with SPEAKER_MODEL in jarvis/core/config.py
SPK       := models/wespeaker-voxceleb-resnet34-LM.onnx
SPK_URL   := https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx
SPK_SHA   := 7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068
WEB_PORT  := 8787
# Linux LiveKit server: the pinned release binary, hash-checked, into ~/.local/bin. Not get.livekit.io: it wants sudo
# for /usr/local/bin and always takes the latest release. Keep in step with the macOS one (brew info livekit).
LIVEKIT_VERSION := 1.13.7
LIVEKIT_URL     := https://github.com/livekit/livekit/releases/download/v$(LIVEKIT_VERSION)
LIVEKIT_SHA_amd64 := 6634aeeb2fb1366b6723708ae4320b9d5408106a4c63457c5e845ae3979c90e2
LIVEKIT_SHA_arm64 := 5d167fdf52cf43c0c72972f25325364479f41f854bfef651056eab2504da5de9
# macOS without Homebrew (no admin rights): LiveKit ships no macOS binaries, so build the pinned source with a pinned
# Go, both hash-checked and fetched into a temp folder. Go must satisfy the go line in LiveKit's go.mod.
LIVEKIT_SRC_SHA := b42f34b095dff22639a40256c3f98fd563fdbce5d497e449ceb06ee010de88c5
GO_VERSION      := 1.26.9
GO_SHA_amd64    := 00c29e3d4c8562f547410daa18d2959b641bd691ab00c4a525ea817a0253d4bd
GO_SHA_arm64    := f9bb7c0a02506c5d9bf0d1eb1f7ee6c7684f844ae49558308a0427b830e022cc
LOCAL_BIN       := $(HOME)/.local/bin
LIVEKIT_SERVER   = $(or $(shell command -v livekit-server),$(LOCAL_BIN)/livekit-server)
# Pinned so a new pyright release can't break make check
PYRIGHT   := 1.1.414
NPM       := npm --prefix web
# web-install reruns only when the manifests change
WEB_STAMP := web/node_modules/.install-stamp

.PHONY: help install uv-bin livekit-bin models models-int8 setup console livekit web web-api web-install web-build web-dev agent runner dev dev-hot tasks speech-gate voice-timing voiceprint test host-check artifact-check \
        services-install services-start services-stop services-restart status logs awake sleep-normal \
        claude-login claude-status accounts about servers passcode vault chat-check tailscale-serve check clean \
        node-setup node-login node-pair node-install node-stop node-check node-status

help: ## Show this list
	@grep -hE '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ── First-time setup ───────────────────────────────────────────

install: uv-bin ## Install tools, dependencies and models
ifeq ($(UNAME),Darwin)
	@command -v livekit-server >/dev/null || [ -x $(LOCAL_BIN)/livekit-server ] || \
		if command -v brew >/dev/null; then brew install livekit; else $(MAKE) --no-print-directory livekit-bin; fi
else
	@command -v livekit-server >/dev/null || [ -x $(LOCAL_BIN)/livekit-server ] || $(MAKE) --no-print-directory livekit-bin
endif
	$(uv) sync
	$(MAKE) models web-build

uv-bin: ## Install the pinned uv into ~/.local/bin if no working one is found (no sudo)
	@uv --version >/dev/null 2>&1 || { set -o pipefail; curl -LsSf https://astral.sh/uv/$(UV_VERSION)/install.sh | UV_INSTALL_DIR=$(HOME)/.local/bin sh; }

livekit-bin: ## Install livekit-server into ~/.local/bin (built from source on macOS, about a minute)
ifeq ($(UNAME),Darwin)
	@case $$(uname -m) in x86_64) arch=amd64 sha=$(GO_SHA_amd64);; arm64) arch=arm64 sha=$(GO_SHA_arm64);; \
		*) echo "no Go $(GO_VERSION) build for $$(uname -m)"; exit 1;; esac; \
	tmp=$$(mktemp -d) && trap 'rm -rf "$$tmp"' EXIT && \
	curl -fSL -o $$tmp/go.tar.gz https://go.dev/dl/go$(GO_VERSION).darwin-$$arch.tar.gz && \
	echo "$$sha  $$tmp/go.tar.gz" | $(SHA256) -c --quiet - && \
	curl -fSL -o $$tmp/livekit.tar.gz https://github.com/livekit/livekit/archive/refs/tags/v$(LIVEKIT_VERSION).tar.gz && \
	echo "$(LIVEKIT_SRC_SHA)  $$tmp/livekit.tar.gz" | $(SHA256) -c --quiet - && \
	tar -xzf $$tmp/go.tar.gz -C $$tmp && tar -xzf $$tmp/livekit.tar.gz -C $$tmp && \
	echo "building livekit-server $(LIVEKIT_VERSION)..." && \
	(cd $$tmp/livekit-$(LIVEKIT_VERSION) && GOTOOLCHAIN=local GOFLAGS=-modcacherw GOPATH=$$tmp/gopath \
		GOCACHE=$$tmp/gocache $$tmp/go/bin/go build -trimpath -o $$tmp/livekit-server ./cmd/server) && \
	mkdir -p $(LOCAL_BIN) && install -m 755 $$tmp/livekit-server $(LOCAL_BIN)/livekit-server && \
	echo "livekit-server $(LIVEKIT_VERSION) installed in $(LOCAL_BIN)"
else
	@case $$(uname -m) in x86_64|amd64) arch=amd64 sha=$(LIVEKIT_SHA_amd64);; aarch64|arm64) arch=arm64 sha=$(LIVEKIT_SHA_arm64);; \
		*) echo "no LiveKit $(LIVEKIT_VERSION) build for $$(uname -m)"; exit 1;; esac; \
	tmp=$$(mktemp -d) && trap 'rm -rf "$$tmp"' EXIT && \
	curl -fSL -o $$tmp/livekit.tar.gz $(LIVEKIT_URL)/livekit_$(LIVEKIT_VERSION)_linux_$$arch.tar.gz && \
	echo "$$sha  $$tmp/livekit.tar.gz" | sha256sum -c --quiet - && \
	tar -xzf $$tmp/livekit.tar.gz -C $$tmp livekit-server && \
	mkdir -p $(LOCAL_BIN) && install -m 755 $$tmp/livekit-server $(LOCAL_BIN)/livekit-server && \
	echo "livekit-server $(LIVEKIT_VERSION) installed in $(LOCAL_BIN)"
endif

# shasum on macOS; sha256sum where Perl's shasum isn't installed
SHA256 = $$(command -v shasum >/dev/null && echo shasum -a 256 || echo sha256sum)
# $(call fetch,FILE,URL,SHA): download once into FILE.part, keep it only if the hash matches
fetch = [ -f $(1) ] || { curl -fSL -o $(1).part $(2) && echo "$(3)  $(1).part" | $(SHA256) -c --quiet - && mv $(1).part $(1); }

models: ## Download speech, VAD, noise and speaker models
	@mkdir -p models
	@$(call fetch,$(TTS),$(KOKORO_HF)/kokoro-v1.0.onnx,$(TTS_SHA))
	@$(call fetch,$(VOICES),$(KOKORO_HF)/voices-v1.0.bin,$(VOICES_SHA))
	@$(call fetch,$(DFN),$(DFN_URL),$(DFN_SHA))
	@$(call fetch,$(SPK),$(SPK_URL),$(SPK_SHA))
# Whisper for this machine: MLX on Apple silicon, faster-whisper elsewhere (jarvis.core.whisper)
	$(UV) python -m jarvis.core.whisper
# os._exit skips teardown, which native plugin libraries can crash on macOS
	$(UV) python -c "import os, sys; from livekit.agents.__main__ import main; code = main(['download-files']); \
		sys.stdout.flush(); sys.stderr.flush(); os._exit(code)"

models-int8: ## Download the 8-bit Kokoro (~230 MB less memory, slower; JARVIS_TTS_MODEL=kokoro-v1.0.int8.onnx)
	@mkdir -p models
	@[ -f models/kokoro-v1.0.int8.onnx ] || { curl -fSL -o models/kokoro-v1.0.int8.onnx.part $(KOKORO_INT8) && \
		mv models/kokoro-v1.0.int8.onnx.part models/kokoro-v1.0.int8.onnx; }

setup: uv-bin ## Generate keys, passcode and LiveKit config (rerunnable)
	$(UV) python -m jarvis.setup_local

claude-login: ## Log in to Claude (yours, or JARVIS's own NAME=work), check, restart services; also in Settings
	@cd "$(MAIN_DIR)" && $(JARVIS_PYTHON) -m jarvis.claude_login login $(NAME)

claude-check: ## Check JARVIS's services can use a Claude login (NAME=work; default: yours)
	@cd "$(MAIN_DIR)" && $(JARVIS_PYTHON) -m jarvis.claude_login check $(NAME)

claude-status: ## Show the Claude account JARVIS uses
	@$(if $(CLAUDE_PATH),CLAUDE_CONFIG_DIR="$(CLAUDE_PATH)" )claude auth status | grep -E '"(loggedIn|email|orgName|subscriptionType)"'

accounts: ## Google accounts (ARGS="add home ...")
	$(UV) python -m jarvis.integrations.accounts $(or $(ARGS),list)

about: ## Show "about you" text (ARGS="--from me.txt")
	@$(UV) python -m jarvis.state.settings about $(ARGS)

servers: ## MCP servers (ARGS="set-secret notion")
	@$(UV) python -m jarvis.integrations.mcp_servers $(or $(ARGS),list)

passcode: ## Show the logins' passcodes (this computer only; never over the web)
	@$(UV) python -m jarvis.core.logins

vault: ## Vault status (ARGS=init|rotate|reencrypt|"reset --yes")
	@$(UV) python -m jarvis.core.vault $(or $(ARGS),status)

chat-check: ## Check Google Chat access (LABEL=...)
	$(UV) python -m jarvis.integrations.accounts check-chat $(LABEL)

tailscale-serve: ## Serve web app and LiveKit on the tailnet
	tailscale serve --bg $(WEB_PORT)
	tailscale serve --bg --set-path /rtc http://127.0.0.1:7880/rtc
	tailscale serve status

## ── Run in the foreground ──────────────────────────────────────

# Exits 0 when a task runner answers on its socket. Checked first: a second runner would take the socket over
RUNNER_UP = import socket, sys; from jarvis.core.config import RUNNER_SOCKET as p; \
	sys.exit(not p.exists() or socket.socket(socket.AF_UNIX).connect_ex(str(p)))

console: ## Talk via this machine's mic, no server (starts the task runner for the session if it isn't running)
	@mkdir -p logs; \
	if $(UV) python -c "$(RUNNER_UP)" 2>/dev/null; then echo "using the task runner that's already running"; \
	else echo "starting the task runner for this session (logs/console-runner.log)"; \
		$(UV) python -m jarvis.tasks.runner >logs/console-runner.log 2>&1 & runner=$$!; \
		trap 'kill -TERM $$runner 2>/dev/null; wait $$runner 2>/dev/null' EXIT INT TERM; fi; \
	$(UV) python -m jarvis.voice.agent console

livekit: ## Run LiveKit server
	$(LIVEKIT_SERVER) --config secrets/livekit.yaml

web: web-build ## Build and run web app (:8787)
	$(UV) python -m jarvis.server

web-dev: $(WEB_STAMP) ## Web app with hot reload (:5173, API via make web; API=https://host for another JARVIS)
	JARVIS_WEB_PORT=$(WEB_PORT) JARVIS_API=$(API) $(NPM) run dev

agent: ## Run voice agent with reload
	$(UV) python -m jarvis.voice.agent dev

runner: ## Run task runner
	$(UV) python -m jarvis.tasks.runner

dev: ## Run LiveKit, web, agent and runner together
	@$(MAKE) -j4 --no-print-directory livekit web agent runner

dev-hot: ## Like dev, but live: UI on :5173 updates as you save; web server and agent restart on Python changes
	@$(MAKE) -j5 --no-print-directory livekit web-api agent runner web-dev

web-api: ## Web server only, restarted when jarvis/ changes (the UI comes from make web-dev)
	$(UV) watchfiles --filter python "python -m jarvis.server" jarvis

## ── 24/7 services (launchd on macOS, systemd on Linux) ────────

services-install: setup web-build ## Install and start the services
	$(UV) python -m jarvis.setup_local --install

# Both also handle services under stale labels (e.g. an old prefix): stop unloads them, start replaces them
services-start: ## Start services
	@$(SERVICE_CTL) start

services-stop: ## Stop services (stay installed)
	@$(SERVICE_CTL) stop; true

# Stop+start run detached in their own session (setsid): stopping a service kills its children, so a restart started
# by the agent or a task would die halfway. Not a child either (`& wait` still died), so poll a done file. On Linux
# setsid isn't enough (systemd stops a service's whole cgroup), so systemd-run starts the restart as its own unit.
RESTART_LOG := $(MAIN_DIR)/logs/restart.log
ifeq ($(UNAME),Darwin)
DETACH := /usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV'
else
DETACH  = $(if $(shell command -v systemd-run),systemd-run --user --collect --quiet --setenv=PATH="$$PATH" --,setsid)
endif

services-restart: web-build ## Rebuild web app, restart services
	@mkdir -p "$(MAIN_DIR)/logs"; rm -f "$(RESTART_LOG).done"
	@$(DETACH) \
		sh -c '$(MAKE) --no-print-directory -C "$(MAIN_DIR)" services-stop services-start > "$(RESTART_LOG)" 2>&1; touch "$(RESTART_LOG).done"' \
		</dev/null >/dev/null 2>&1 &
	@for i in $$(seq 180); do [ -f "$(RESTART_LOG).done" ] && break; sleep 1; done; cat "$(RESTART_LOG)"; \
		[ -f "$(RESTART_LOG).done" ] || echo "still restarting, see $(RESTART_LOG)"

status: ## Show service status
ifeq ($(UNAME),Darwin)
	@for s in $(SERVICES); do \
		pid=$$(launchctl list | awk -v s=$$s '$$3 == s {print $$1}'); \
		printf "  %-26s %s\n" $$s "$${pid:-not loaded}"; done
else
	@$(SERVICE_CTL) status
endif

logs: ## Follow service logs
	@mkdir -p logs && touch logs/livekit.log logs/agent.log logs/web.log logs/tasks.log
	tail -f logs/livekit.log logs/agent.log logs/web.log logs/tasks.log

# Linux: masking the sleep targets makes suspend (lid close included) a no-op. Under WSL2 Windows decides; no effect.
SLEEP_TARGETS := sleep.target suspend.target hibernate.target hybrid-sleep.target

awake: ## Stay awake with lid closed (sudo)
ifeq ($(UNAME),Darwin)
	sudo pmset -a disablesleep 1
else
	sudo systemctl mask $(SLEEP_TARGETS)
endif

sleep-normal: ## Undo make awake
ifeq ($(UNAME),Darwin)
	sudo pmset -a disablesleep 0
else
	sudo systemctl unmask $(SLEEP_TARGETS)
endif

## ── Background tasks ─────────────────────────────────────────

tasks: ## List recent tasks
	@$(UV) python -m jarvis.tasks.runner list

speech-gate: ## Clips the speech gate dropped (last day)
	@$(UV) python -m jarvis.voice.speech_gate

voice-timing: ## Voice turn latency by stage (HOURS=24)
	@$(UV) python -m jarvis.voice.timing $(HOURS)

voiceprint: ## Voiceprint (ARGS=enroll|test|remove|log)
	@$(UV) python -m jarvis.voice.speaker $(or $(ARGS),status)

## ── A second machine that takes JARVIS's heavy work (jarvis.node) ──

node-setup: ## On a second machine: Python dependencies only (no voice models or web app)
	uv sync
	@echo "Next: make node-login, then pair from Settings › Machines on the main computer"

node-login: ## On a second machine: log in its own Claude account
	CLAUDE_CONFIG_DIR=$(CLAUDE_DIR) claude auth login

node-pair: ## On a second machine: pair with the main computer (CODE=from Settings › Machines MAIN=https://host)
	@[ -n "$(CODE)" ] && [ -n "$(MAIN)" ] || { echo "make node-pair CODE=... MAIN=https://<main computer>"; exit 2; }
	$(UV) python -m jarvis.node pair "$(CODE)" "$(MAIN)"

node-install: ## On a second machine: install and start its service
	$(UV) python -m jarvis.setup_local node-install

node-stop: ## On a second machine: stop its service (stays installed)
	@$(UV) python -m jarvis.setup_local node-stop; true

node-check: ## On a second machine: walls, pairing, link to the main computer, tools
	$(UV) python -m jarvis.node check

node-status: ## On a second machine: its pairing and both fingerprints
	@$(UV) python -m jarvis.node status

## ── Web app (web/: Vite, TypeScript, Preact) ───────────────────

web-install: $(WEB_STAMP) ## Install web Node packages

$(WEB_STAMP): web/package.json web/package-lock.json
	$(NPM) install
	@touch $@

web-build: $(WEB_STAMP) ## Build web app into web/dist
	$(NPM) run build

## ── Housekeeping ───────────────────────────────────────────────

test: ## Run unit tests
	$(UV) --with pytest pytest -q

host-check: ## Check task sandbox walls
	$(UV) python -m jarvis.safety.host check

artifact-check: ## Test artifact publishing (FILE=page.html)
	$(if $(CLAUDE_PATH),JARVIS_CLAUDE_CONFIG_DIR="$(CLAUDE_PATH)" )$(JARVIS_PYTHON) -m jarvis.tasks.artifact_check $(if $(FILE),--file "$(abspath $(FILE))")

check: ## Compile, type-check, import all
	$(UV) python -m compileall -q jarvis
# Errors only; see warnings with `uv run --with pyright pyright`
	$(UV) --with pyright==$(PYRIGHT) pyright --level error
	$(NPM) run typecheck
	$(UV) python -c "import jarvis.voice.agent, jarvis.server, jarvis.setup_local, jarvis.integrations.accounts, jarvis.state.settings, jarvis.brain.cli, jarvis.tasks.runner, jarvis.tasks.notices; from jarvis import tools; print(len(tools.all_tools()), 'tools, imports ok')"

clean: ## Remove caches and logs
	find . -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
	rm -rf logs/*.log
