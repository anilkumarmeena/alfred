# JARVIS: common tasks. Run `make` for the list.

SHELL := /bin/bash
.DEFAULT_GOAL := help
unexport VIRTUAL_ENV  # stops uv warning about a shell-wide venv

UV        := uv run
# Main checkout, even when run from a task's worktree (data/worktrees/jarvis-N)
MAIN_DIR  := $(or $(patsubst %/.git,%,$(shell git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)),$(CURDIR))
# Main checkout's Python (services, artifact-check), also from a worktree. Override with JARVIS_PYTHON=...
ifeq ($(realpath $(MAIN_DIR)),$(realpath $(CURDIR)))
JARVIS_PYTHON ?= $(UV) python
else
JARVIS_PYTHON ?= $(MAIN_DIR)/.venv/bin/python
endif
# launchd labels (com.jarvis.* or JARVIS_SERVICE_PREFIX), evaluated lazily. Service control always runs in the main
# checkout so a worktree never installs its own copy.
SERVICE_CTL = cd $(MAIN_DIR) && $(JARVIS_PYTHON) -m jarvis.setup_local
SERVICES    = $(shell $(SERVICE_CTL) labels)
CLAUDE_DIR := $(MAIN_DIR)/secrets/claude
KOKORO_URL := https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
# Whisper large-v3-turbo, 4-bit; keep in step with STT_MODEL in jarvis/core/config.py
WHISPER   := mlx-community/whisper-large-v3-turbo-q4
# Noise filter: DeepFilterNet 3 streaming ONNX (torchDF export), pinned and hash-checked
DFN       := models/deepfilternet3-streaming.onnx
DFN_URL   := https://github.com/yuyun2000/SpeechDenoiser/raw/9a1f069f562089c2f56008a8ecd897fa981fbc50/48k/denoiser_model.onnx
DFN_SHA   := fe5eb64fa2e4154c83f8e4935e82871c850c154387ee892e0ab65fe179e7d8c9
# Speaker check: WeSpeaker ResNet34, pinned and hash-checked; keep in step with SPEAKER_MODEL in jarvis/core/config.py
SPK       := models/wespeaker-voxceleb-resnet34-LM.onnx
SPK_URL   := https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx
SPK_SHA   := 7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068
WEB_PORT  := 8787
# Pinned so a new pyright release can't break make check
PYRIGHT   := 1.1.414
NPM       := npm --prefix web
# web-install reruns only when the manifests change
WEB_STAMP := web/node_modules/.install-stamp

.PHONY: help install models setup console livekit web web-install web-build web-dev agent runner dev tasks heartbeat speech-gate voice-timing voiceprint test host-check artifact-check \
        services-install services-start services-stop services-restart status logs awake sleep-normal \
        claude-login claude-status accounts about servers vault google-login chat-check tailscale-serve check clean

help: ## Show this list
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ── First-time setup ───────────────────────────────────────────

install: ## Install tools, dependencies and models
	@command -v livekit-server >/dev/null || brew install livekit
	uv sync
	$(MAKE) models web-build

models: ## Download speech, VAD, noise and speaker models
	@mkdir -p models
	@for f in kokoro-v1.0.int8.onnx kokoro-v1.0.onnx voices-v1.0.bin; do \
		[ -f models/$$f ] || curl -fSL -o models/$$f $(KOKORO_URL)/$$f; done
	@[ -f $(DFN) ] || { curl -fSL -o $(DFN).part $(DFN_URL) && \
		echo "$(DFN_SHA)  $(DFN).part" | shasum -a 256 -c --quiet - && mv $(DFN).part $(DFN); }
	@[ -f $(SPK) ] || { curl -fSL -o $(SPK).part $(SPK_URL) && \
		echo "$(SPK_SHA)  $(SPK).part" | shasum -a 256 -c --quiet - && mv $(SPK).part $(SPK); }
	$(UV) python -c "from huggingface_hub import snapshot_download; snapshot_download('$(WHISPER)')"
# os._exit skips teardown, which native plugin libraries can crash on macOS
	$(UV) python -c "import os, sys; from livekit.agents.__main__ import main; code = main(['download-files']); \
		sys.stdout.flush(); sys.stderr.flush(); os._exit(code)"

setup: ## Generate keys, passcode and LiveKit config (rerunnable)
	$(UV) python -m jarvis.setup_local

claude-login: ## Log in JARVIS's own Claude CLI
	CLAUDE_CONFIG_DIR=$(CLAUDE_DIR) claude auth login

claude-status: ## Show JARVIS's Claude account
	@CLAUDE_CONFIG_DIR=$(CLAUDE_DIR) claude auth status | grep -E '"(loggedIn|email|orgName|subscriptionType)"'

accounts: ## Google accounts (ARGS="add home ...")
	$(UV) python -m jarvis.integrations.accounts $(or $(ARGS),list)

about: ## Show "about you" text (ARGS="--from me.txt")
	@$(UV) python -m jarvis.state.settings about $(ARGS)

servers: ## MCP servers (ARGS="set-secret notion")
	@$(UV) python -m jarvis.integrations.mcp_servers $(or $(ARGS),list)

vault: ## Vault status (ARGS=init|rotate|reencrypt|"reset --yes")
	@$(UV) python -m jarvis.core.vault $(or $(ARGS),status)

google-login: ## Log in a Google account (LABEL=work)
	$(UV) python -m jarvis.integrations.accounts login $(LABEL)

chat-check: ## Check Google Chat access (LABEL=...)
	$(UV) python -m jarvis.integrations.accounts check-chat $(LABEL)

tailscale-serve: ## Serve web app and LiveKit on the tailnet
	tailscale serve --bg $(WEB_PORT)
	tailscale serve --bg --set-path /rtc http://127.0.0.1:7880/rtc
	tailscale serve status

## ── Run in the foreground ──────────────────────────────────────

console: ## Talk via the Mac's mic, no server
	$(UV) python -m jarvis.voice.agent console

livekit: ## Run LiveKit server
	livekit-server --config secrets/livekit.yaml

web: web-build ## Build and run web app (:8787)
	$(UV) python -m jarvis.server

web-dev: $(WEB_STAMP) ## Web app with hot reload (:5173, API via make web)
	JARVIS_WEB_PORT=$(WEB_PORT) $(NPM) run dev

agent: ## Run voice agent with reload
	$(UV) python -m jarvis.voice.agent dev

runner: ## Run task runner
	$(UV) python -m jarvis.tasks.runner

dev: ## Run LiveKit, web, agent and runner together
	@$(MAKE) -j4 --no-print-directory livekit web agent runner

## ── 24/7 services (launchd) ────────────────────────────────────

services-install: setup web-build ## Install and start launchd services
	$(UV) python -m jarvis.setup_local --install

# Both also handle services under stale labels (e.g. an old prefix): stop unloads them, start replaces them
services-start: ## Start services
	@$(SERVICE_CTL) start

services-stop: ## Stop services (stay installed)
	@$(SERVICE_CTL) stop; true

# Stop+start run detached in their own session (setsid): stopping a service kills its children, so a restart started
# by the agent or a task would die halfway. Not a child either (`& wait` still died), so poll a done file.
RESTART_LOG := $(MAIN_DIR)/logs/restart.log

services-restart: web-build ## Rebuild web app, restart services
	@mkdir -p $(MAIN_DIR)/logs; rm -f $(RESTART_LOG).done
	@/usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV' \
		sh -c '$(MAKE) --no-print-directory -C $(MAIN_DIR) services-stop services-start > $(RESTART_LOG) 2>&1; touch $(RESTART_LOG).done' \
		</dev/null >/dev/null 2>&1 &
	@for i in $$(seq 180); do [ -f $(RESTART_LOG).done ] && break; sleep 1; done; cat $(RESTART_LOG); \
		[ -f $(RESTART_LOG).done ] || echo "still restarting, see $(RESTART_LOG)"

status: ## Show service status
	@for s in $(SERVICES); do \
		pid=$$(launchctl list | awk -v s=$$s '$$3 == s {print $$1}'); \
		printf "  %-26s %s\n" $$s "$${pid:-not loaded}"; done

logs: ## Follow service logs
	@mkdir -p logs && touch logs/livekit.log logs/agent.log logs/web.log logs/tasks.log
	tail -f logs/livekit.log logs/agent.log logs/web.log logs/tasks.log

awake: ## Stay awake with lid closed (sudo)
	sudo pmset -a disablesleep 1

sleep-normal: ## Undo make awake
	sudo pmset -a disablesleep 0

## ── Background tasks ─────────────────────────────────────────

tasks: ## List recent tasks
	@$(UV) python -m jarvis.tasks.runner list

heartbeat: ## Dry-run one heartbeat
	$(UV) python -m jarvis.tasks.heartbeat

speech-gate: ## Clips the speech gate dropped (last day)
	@$(UV) python -m jarvis.voice.speech_gate

voice-timing: ## Voice turn latency by stage (HOURS=24)
	@$(UV) python -m jarvis.voice.timing $(HOURS)

voiceprint: ## Voiceprint (ARGS=enroll|test|remove|log)
	@$(UV) python -m jarvis.voice.speaker $(or $(ARGS),status)

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
	JARVIS_CLAUDE_CONFIG_DIR="$${JARVIS_CLAUDE_CONFIG_DIR:-$(CLAUDE_DIR)}" \
		$(JARVIS_PYTHON) -m jarvis.tasks.artifact_check $(if $(FILE),--file $(abspath $(FILE)))

check: ## Compile, type-check, import all
	$(UV) python -m compileall -q jarvis
# Errors only; see warnings with `uv run --with pyright pyright`
	$(UV) --with pyright==$(PYRIGHT) pyright --level error
	$(NPM) run typecheck
	$(UV) python -c "import jarvis.voice.agent, jarvis.server, jarvis.setup_local, jarvis.integrations.accounts, jarvis.state.settings, jarvis.brain.cli, jarvis.tasks.runner, jarvis.tasks.heartbeat; from jarvis import tools; print(len(tools.all_tools()), 'tools, imports ok')"

clean: ## Remove caches and logs
	find . -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
	rm -rf logs/*.log
