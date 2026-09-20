# JARVIS: common tasks. Run `make` for the list.

SHELL := /bin/bash
.DEFAULT_GOAL := help
unexport VIRTUAL_ENV  # a shell-wide venv would otherwise make uv warn on every call

UV        := uv run
# The main JARVIS checkout, also when make runs in a task's worktree (data/worktrees/jarvis-N)
MAIN_DIR  := $(or $(patsubst %/.git,%,$(shell git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)),$(CURDIR))
# Python for what runs on the main checkout's code and login (the services, artifact-check): its .venv, also from a
# task's worktree. JARVIS_PYTHON=... overrides it.
ifeq ($(realpath $(MAIN_DIR)),$(realpath $(CURDIR)))
JARVIS_PYTHON ?= $(UV) python
else
JARVIS_PYTHON ?= $(MAIN_DIR)/.venv/bin/python
endif
# The launchd services' labels, com.jarvis.agent and so on (JARVIS_SERVICE_PREFIX in .env changes the com.jarvis), as
# the main checkout has them; worked out only when a target uses them. Starting and stopping run in the main checkout
# too, so a task's worktree never installs its own copy of a service.
SERVICE_CTL = cd $(MAIN_DIR) && $(JARVIS_PYTHON) -m jarvis.setup_local
SERVICES    = $(shell $(SERVICE_CTL) labels)
CLAUDE_DIR := $(MAIN_DIR)/secrets/claude
KOKORO_URL := https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0
# Whisper large-v3-turbo, 4-bit; keep in step with STT_MODEL in jarvis/core/config.py
WHISPER   := mlx-community/whisper-large-v3-turbo-q4
# DeepFilterNet 3 as one streaming ONNX file (grazder's torchDF export), pinned to a commit and checked by hash
DFN       := models/deepfilternet3-streaming.onnx
DFN_URL   := https://github.com/yuyun2000/SpeechDenoiser/raw/9a1f069f562089c2f56008a8ecd897fa981fbc50/48k/denoiser_model.onnx
DFN_SHA   := fe5eb64fa2e4154c83f8e4935e82871c850c154387ee892e0ab65fe179e7d8c9
# The speaker check's model (jarvis.voice.speaker): WeSpeaker ResNet34 from Hugging Face, pinned to a commit and checked
# by hash; keep in step with SPEAKER_MODEL in jarvis/core/config.py
SPK       := models/wespeaker-voxceleb-resnet34-LM.onnx
SPK_URL   := https://huggingface.co/Wespeaker/wespeaker-voxceleb-resnet34-LM/resolve/f0c48c298fd835726c27956a5d617bad7115627e/voxceleb_resnet34_LM.onnx
SPK_SHA   := 7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068
WEB_PORT  := 8787
# The type checker make check runs, pinned so a new release's rules don't fail it unasked
PYRIGHT   := 1.1.414
NPM       := npm --prefix web
# Stamp: web-install reinstalls only when the manifests change.
WEB_STAMP := web/node_modules/.install-stamp

.PHONY: help install models setup console livekit web web-install web-build web-dev agent runner dev tasks heartbeat speech-gate voice-timing voiceprint test host-check artifact-check \
        services-install services-start services-stop services-restart status logs awake sleep-normal \
        claude-login claude-status accounts about servers vault google-login chat-check tailscale-serve check clean

help: ## Show this list
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

## ── First-time setup ───────────────────────────────────────────

install: ## Install tools and Python dependencies, then download models
	@command -v livekit-server >/dev/null || brew install livekit
	@command -v github-mcp-server >/dev/null || brew install github-mcp-server
	uv sync
	$(MAKE) models web-build

models: ## Download Kokoro, Whisper, VAD, turn-detector, noise-filter and speaker models
	@mkdir -p models
	@for f in kokoro-v1.0.int8.onnx kokoro-v1.0.onnx voices-v1.0.bin; do \
		[ -f models/$$f ] || curl -fSL -o models/$$f $(KOKORO_URL)/$$f; done
	@[ -f $(DFN) ] || { curl -fSL -o $(DFN).part $(DFN_URL) && \
		echo "$(DFN_SHA)  $(DFN).part" | shasum -a 256 -c --quiet - && mv $(DFN).part $(DFN); }
	@[ -f $(SPK) ] || { curl -fSL -o $(SPK).part $(SPK_URL) && \
		echo "$(SPK_SHA)  $(SPK).part" | shasum -a 256 -c --quiet - && mv $(SPK).part $(SPK); }
	$(UV) python -c "from huggingface_hub import snapshot_download; snapshot_download('$(WHISPER)')"
# Exits with the downloads' own status, skipping teardown, which the plugins' native libraries can abort on macOS
	$(UV) python -c "import os, sys; from livekit.agents.__main__ import main; code = main(['download-files']); \
		sys.stdout.flush(); sys.stderr.flush(); os._exit(code)"

setup: ## Generate LiveKit keys, phone passcode and LiveKit config (safe to rerun)
	$(UV) python -m jarvis.setup_local

claude-login: ## Sign JARVIS's own Claude CLI login in (separate from your Claude Code)
	CLAUDE_CONFIG_DIR=$(CLAUDE_DIR) claude auth login

claude-status: ## Show which account JARVIS's Claude login uses
	@CLAUDE_CONFIG_DIR=$(CLAUDE_DIR) claude auth status | grep -E '"(loggedIn|email|orgName|subscriptionType)"'

accounts: ## List the Google accounts; change them with ARGS, e.g. make accounts ARGS="add home --note 'my own'"
	$(UV) python -m jarvis.integrations.accounts $(or $(ARGS),list)

about: ## Show the "about you" text JARVIS's prompt passes on; change it with ARGS, e.g. make about ARGS="--from me.txt"
	@$(UV) python -m jarvis.state.settings about $(ARGS)

servers: ## List the MCP servers (connections: GitHub, Notion, ...); change them with ARGS, e.g. make servers ARGS="set-secret notion"
	@$(UV) python -m jarvis.integrations.mcp_servers $(or $(ARGS),list)

vault: ## The encrypted secrets' key and names (never values); ARGS=init, rotate, reencrypt or "reset --yes"
	@$(UV) python -m jarvis.core.vault $(or $(ARGS),status)

google-login: ## Sign a Google account in, or in again for new permissions: make google-login LABEL=work
	$(UV) python -m jarvis.integrations.accounts login $(LABEL)

chat-check: ## Check Google Chat works (LABEL=..., else the first account with Chat), and say what to fix if not
	$(UV) python -m jarvis.integrations.accounts check-chat $(LABEL)

tailscale-serve: ## Publish the web app and LiveKit signalling on the tailnet over HTTPS
	tailscale serve --bg $(WEB_PORT)
	tailscale serve --bg --set-path /rtc http://127.0.0.1:7880/rtc
	tailscale serve status

## ── Run in the foreground ──────────────────────────────────────

console: ## Talk to JARVIS in this terminal through the Mac's mic (no server needed)
	$(UV) python -m jarvis.voice.agent console

livekit: ## Run the local LiveKit server
	livekit-server --config secrets/livekit.yaml

web: web-build ## Build and run the web app on http://localhost:8787
	$(UV) python -m jarvis.server

web-dev: $(WEB_STAMP) ## Run the web app with hot reload on http://localhost:5173 (API calls go to `make web`)
	JARVIS_WEB_PORT=$(WEB_PORT) $(NPM) run dev

agent: ## Run the voice agent with auto-reload
	$(UV) python -m jarvis.voice.agent dev

runner: ## Run the task runner, which does background tasks
	$(UV) python -m jarvis.tasks.runner

dev: ## Run LiveKit, the web app, the agent and the task runner together (Ctrl-C stops all)
	@$(MAKE) -j4 --no-print-directory livekit web agent runner

## ── 24/7 services (launchd) ────────────────────────────────────

services-install: setup web-build ## Install and start the four launchd services
	$(UV) python -m jarvis.setup_local --install

# Both also move an install off labels it no longer uses, such as an older prefix (jarvis.setup_local): stop unloads
# those too, and start removes them and installs the current ones in their place
services-start: ## Start the installed services
	@$(SERVICE_CTL) start

services-stop: ## Stop the services (they stay installed)
	@$(SERVICE_CTL) stop; true

# The stop and start run in their own session (setsid): stopping a service kills everything it started, so a restart
# asked for by the agent's shell or a task would otherwise die halfway and leave the services down. The shell that
# starts it exits at once, so it is nobody's child either: a version that kept it as a child (`& wait $$!`) still died
# with the agent. So this polls for a done file instead of waiting on it.
RESTART_LOG := $(MAIN_DIR)/logs/restart.log

services-restart: web-build ## Rebuild the web app and restart the services, e.g. after editing .env
	@mkdir -p $(MAIN_DIR)/logs; rm -f $(RESTART_LOG).done
	@/usr/bin/perl -MPOSIX -e 'POSIX::setsid(); exec @ARGV' \
		sh -c '$(MAKE) --no-print-directory -C $(MAIN_DIR) services-stop services-start > $(RESTART_LOG) 2>&1; touch $(RESTART_LOG).done' \
		</dev/null >/dev/null 2>&1 &
	@for i in $$(seq 180); do [ -f $(RESTART_LOG).done ] && break; sleep 1; done; cat $(RESTART_LOG); \
		[ -f $(RESTART_LOG).done ] || echo "still restarting, see $(RESTART_LOG)"

status: ## Show whether each service is running
	@for s in $(SERVICES); do \
		pid=$$(launchctl list | awk -v s=$$s '$$3 == s {print $$1}'); \
		printf "  %-26s %s\n" $$s "$${pid:-not loaded}"; done

logs: ## Follow all service logs
	@mkdir -p logs && touch logs/livekit.log logs/agent.log logs/web.log logs/tasks.log
	tail -f logs/livekit.log logs/agent.log logs/web.log logs/tasks.log

awake: ## Keep the Mac running with the lid closed (asks for sudo; undo with `make sleep-normal`)
	sudo pmset -a disablesleep 1

sleep-normal: ## Let the Mac sleep normally again, e.g. when the lid closes
	sudo pmset -a disablesleep 0

## ── Background tasks ─────────────────────────────────────────

tasks: ## List recent background tasks (show one with: uv run python -m jarvis.tasks.runner show ID)
	@$(UV) python -m jarvis.tasks.runner list

heartbeat: ## Dry-run one heartbeat: print what it would flag now, without notifying or recording it
	$(UV) python -m jarvis.tasks.heartbeat

speech-gate: ## List the last day's voice clips the speech gate would drop, or dropped, and why
	@$(UV) python -m jarvis.voice.speech_gate

voice-timing: ## The last day's voice turns, stage by stage: where the wait before JARVIS speaks goes (HOURS=24)
	@$(UV) python -m jarvis.voice.timing $(HOURS)

voiceprint: ## Your voiceprint for the speaker check; ARGS=enroll, test, remove or log (the last day's verdicts)
	@$(UV) python -m jarvis.voice.speaker $(or $(ARGS),status)

## ── Web app (web/: Vite, TypeScript, Preact) ───────────────────

web-install: $(WEB_STAMP) ## Install the web app's Node packages

$(WEB_STAMP): web/package.json web/package-lock.json
	$(NPM) install
	@touch $@

web-build: $(WEB_STAMP) ## Build the web app into web/dist, which jarvis.server serves
	$(NPM) run build

## ── Housekeeping ───────────────────────────────────────────────

test: ## Run the unit tests
	$(UV) --with pytest pytest -q

host-check: ## Check the walls around tasks' commands on the Mac: secrets blocked, builds and signing still work
	$(UV) python -m jarvis.safety.host check

artifact-check: ## Publish a tiny claude.ai artifact the way tasks do, to check JARVIS's login can (FILE=page.html for a real one)
	JARVIS_CLAUDE_CONFIG_DIR="$${JARVIS_CLAUDE_CONFIG_DIR:-$(CLAUDE_DIR)}" \
		$(JARVIS_PYTHON) -m jarvis.tasks.artifact_check $(if $(FILE),--file $(abspath $(FILE)))

check: ## Byte-compile everything, type-check it (pyright) and import each module
	$(UV) python -m compileall -q jarvis
# Errors only: the warnings pyproject.toml's [tool.pyright] keeps are shown by `uv run --with pyright pyright`
	$(UV) --with pyright==$(PYRIGHT) pyright --level error
	$(NPM) run typecheck
	$(UV) python -c "import jarvis.voice.agent, jarvis.server, jarvis.setup_local, jarvis.integrations.accounts, jarvis.state.settings, jarvis.brain.cli, jarvis.tasks.runner, jarvis.tasks.heartbeat; from jarvis import tools; print(len(tools.all_tools()), 'tools, imports ok')"

clean: ## Remove caches and logs (keeps models, secrets and memory)
	find . -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} +
	rm -rf logs/*.log
