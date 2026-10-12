<div align="center">

<img src="assets/logo.svg" width="72" alt="">

# 👩‍💻 Development

**How the code is laid out, how to test it, and how to work on the web app and AI Armory alongside it.**

[🏠 Home](../README.md) · [🚀 Setup](setup.md) · [⚙️ Configuration](configuration.md) · [🗣️ Voice](voice.md) · [🛠️ Tasks](tasks.md) · [🔌 Integrations](integrations.md) · [📱 Mobile](mobile.md) · **👩‍💻 Development** · [🏗️ Architecture](architecture.md)

</div>

---

> [!TIP]
> This page is about **working on** the code. For how the parts behave at runtime, read **[Architecture](architecture.md)**.

<details>
<summary><b>📑 On this page</b></summary>

- [Code map](#-code-map)
- [Tests and checks](#-tests-and-checks)
- [Running the parts](#-running-the-parts)
- [Web app](#-web-app)
- [Prompts](#-prompts)
- [Adding a tool](#-adding-a-tool)
- [Editing AI Armory alongside](#-editing-ai-armory-alongside)

</details>

## 🧭 Code map

```text
jarvis/
├── voice/           the voice agent (agent.py, session.py), Whisper and Kokoro, turn-taking,
│                    noise filter, speech gate, speaker check, barge-in, timing, audio files
├── brain/           Claude behind the voice: the CLI brain, Agent SDK set-up, persona,
│                    quick routing, side lookups, usage
├── prompts/         the long prompts as markdown, {{name}} placeholders filled by prompts.load()
├── tools/           what the brain can call: Google, Mac (macOS) or desktop (Linux), files,
│                    memory, to-dos, tasks, schedules, browser, skills
├── tasks/           the task runner, its worker and client, projects, notices, scheduler,
│                    learning review
├── safety/          command rules, the host walls (Seatbelt or bubblewrap), approvals, consent,
│                    the gates, hidden-instruction scan and injection check
├── integrations/    Google accounts, MCP servers (registry, launcher, relay, OAuth),
│                    the web browser, sign-ins
├── skills/          skills: storage, installing, script reviews
├── notify/          Web Push, APNs and FCM
├── state/           memory, settings, to-dos, vocabulary, voiceprints, history, pictures
├── core/            config, database, vault, credentials, logins, profiles, documents,
│                    transcription; platform.py (which OS), whisper.py (MLX or
│                    faster-whisper), desktop.py (the Linux desktop)
├── server.py        web app, token endpoint and push registration
├── panel.py         the control panel's actions
└── setup_local.py   local setup and the services (launchd on macOS, systemd on Linux)

web/                 the phone web app (Vite + TypeScript + Preact), served from web/dist
tests/               pytest, plus web/tests/ (vitest)
deploy/              launchd plists, systemd/ user unit templates
docs/                these pages
```

### Start here

| | File | For |
|:-:|---|---|
| 📞 | `jarvis/voice/agent.py`, `jarvis/voice/session.py` | **One call**: LiveKit's side, and the `VoiceSession` |
| 🧠 | `jarvis/brain/cli.py`, `jarvis/brain/sdk.py` | **The brain**: the `claude` CLI through the Agent SDK |
| 🧰 | `jarvis/tools/__init__.py` | **The tool registry**: `@tool` and AI Armory's tool sets |
| ⚙️ | `jarvis/tasks/runner.py`, `jarvis/tasks/worker.py` | **Background tasks**: the runner, its HTTP API and one task's run |
| 🛡️ | `jarvis/safety/` | **Every guard** |
| 🎛️ | `jarvis/core/config.py` | **Every setting** and its default ([Configuration](configuration.md)) |
| 🧩 | `jarvis/core/platform.py` | **Which OS**: `MAC`, `LINUX`, `WSL`, `APPLE_SILICON`, `NAME` ([Platform layer](architecture.md#-platform-layer)) |

- **`VoiceSession`** routes each turn and speaks tasks' requests.
- **`ask_json`** (in the brain) is the one way a short model call gives a verdict: JSON checked against a schema.
- **The guards:** `commands.py` (what's risky), `shell.py` / `draft_gate.py` (spoken yes), `host.py` (the host walls: Seatbelt on macOS, bubblewrap on Linux), `injection.py` / `scan.py`.
- **The Google and Mac tools** come from **[AI Armory](https://github.com/anilkumarmeena/ai-armory)**, a git dependency pinned to a commit in `pyproject.toml`, registered in `jarvis/tools/google.py` and `jarvis/tools/mac.py`. The Linux desktop tools are JARVIS's own: `jarvis/tools/desktop.py` on top of `jarvis/core/desktop.py`.
- **OS differences** stay behind `platform.MAC` / `platform.LINUX` checks; speech goes through `jarvis/core/whisper.py`, which hides whether MLX Whisper or faster-whisper runs.

## 🧪 Tests and checks

| | Command | Runs |
|:-:|---|---|
| 🧪 | `make test` | The pytest suite in `tests/`, plus the web app's vitest tests |
| 🌐 | `npm test --prefix web` | The web app's tests alone |
| ✅ | `make check` | Byte-compiles `jarvis/`, **pyright**, web type-check, imports every entry module |
| 🧱 | `make host-check` | The real sandbox walls around tasks' commands (Seatbelt or bubblewrap) |
| 🖼️ | `make artifact-check` | Publishes a tiny claude.ai artifact on JARVIS's login, the way tasks do |

- **`make test`** fetches pytest with `uv run --with pytest`. The vitest tests are skipped if `web/node_modules` is missing (`make web-install`).
- **Pyright** is a pinned version, errors only. Its softer rules are listed under `[tool.pyright]` in `pyproject.toml`; `uv run --with pyright pyright` shows their warnings.
- **`make check`** passes with **only one speech engine installed**: MLX Whisper on Apple silicon, faster-whisper elsewhere (`pyproject.toml` picks by platform marker), and the other's imports are marked for pyright.
- **`make host-check`** checks that secrets and JARVIS's files are blocked, while PyPI, the task cache and the SSH agent stay reachable, and signing in over SSH works for each SSH git host in your projects' remotes (skipped when none use SSH). Each OS has its own list: macOS adds code signing; Linux adds the session bus, other processes, `sudo` and new files at the top of home.
- **`make artifact-check`** takes `FILE=page.html` to publish a real page.

> [!NOTE]
> Tests use a **scratch data folder** (`tests/conftest.py` sets `JARVIS_DATA_DIR` and `JARVIS_DB_FILE`), so they never touch your data. The voice brain's tests run against a **fake `claude` CLI** (`tests/fake_cli.py`).

#### One suite, two systems

The suite runs on **macOS and Linux**.

| | Kind of test | How |
|:-:|---|---|
| 🔀 | **Platform branches** | Run everywhere by faking the OS: `monkeypatch.setattr(platform, "MAC", False)` and `"LINUX", True` (see `tests/test_desktop.py`, `tests/test_platform.py`) |
| 🍎 | **macOS-only** | `skipif`-marked: real `screencapture`, `sips`, `textutil`, `afconvert`, the Mac tools, memory footprints |
| 🐧 | **Linux-only** | `skipif`-marked: the real bubblewrap walls (`needs_bwrap` in `tests/test_host.py`) |
| 🎤 | **Speech backends** | `tests/test_whisper.py` fakes both MLX Whisper and faster-whisper, so neither needs to be installed |

- **Writing a platform test:** fake `platform.MAC` / `platform.LINUX` (and `WSL` where it matters) rather than skipping, so both branches run on every machine. Skip only when the test needs a real program or OS feature.

## 🏃 Running the parts

| | Command | Runs |
|:-:|---|---|
| 🚀 | `make dev` | LiveKit, web app, agent (auto-reload) and runner together in the foreground |
| 🔥 | `make dev-hot` | The same, live: the UI updates as you save, the web server and agent restart on Python changes |
| 🧩 | `make agent` · `make runner` · `make web` · `make livekit` | One part on its own |
| ⌨️ | `make console` | The agent in the terminal, no LiveKit; starts the task runner for the session if none is running (PortAudio: Linux needs `libportaudio2`) |

These work the same on macOS and Linux; `make livekit` runs the `livekit-server` that `make install` set up (Homebrew on macOS, else `~/.local/bin`).

> [!IMPORTANT]
> Stop the services first (`make services-stop`, which stops the launchd agents or systemd user units) so ports and the runner's socket are free.

## 🌐 Web app

`web/` is a **Vite + TypeScript + Preact** app with signals.

| Path | Holds |
|---|---|
| `src/main.tsx` | Boot: renders the page, wires keys and page lifecycle |
| `src/components/` | One file per part of the screen; `panel/` is the control panel |
| `src/state/` | Signals: `store.ts`, `session.ts` (LiveKit), `push.ts`, `transcript.ts` |
| `src/lib/` | No UI: `mic.ts`, `markdown.ts`, `retry.ts`, `build.ts`, `native.ts`, ... |
| `public/` | Copied as is: `sw.js`, manifest, icons |
| `tests/` | vitest |

- **Keys** wired in `main.tsx`: <kbd>Space</kbd>, <kbd>M</kbd>, <kbd>S</kbd>, <kbd>Esc</kbd>.
- **`store.ts`** is mirrored onto `body[data-*]` for CSS.
- **`mic.ts`** handles earphones and mic recovery; **`markdown.ts`** uses marked + DOMPurify.

| | Task | How |
|:-:|---|---|
| 📦 | **Build** | `make web-build` → `web/dist` |
| 🔥 | **Hot reload** | `make dev-hot`, then open http://localhost:5173 |
| 🏷️ | **Build ids** | Each build carries an id; an older page reloads itself between sessions |

- **Builds:** `make web`, `services-install` and `services-restart` build first; a running server picks up a rebuild without restarting.
- **Hot reload:** http://localhost:5173 serves `web/src` straight from disk and proxies `/api` to the web server on 8787 (`make web-api`, restarted on Python changes). Only the UI piece: `make web-dev` beside a running `make dev` or the services.
- **UI against another JARVIS:** `make web-dev API=https://<host>.<tailnet>.ts.net` runs only the UI here, with hot reload, using that machine's API and LiveKit (through its `make tailscale-serve`). Sign in with that machine's passcode.
- **Build ids** live in `<meta name="jarvis-build">`; the server reports the one it serves.

> [!WARNING]
> **Untrusted text in the panel.** Task summaries, memory and to-dos can come from an email or page a task read. So Markdown is rendered with raw HTML shown as text, DOMPurify keeping only plain formatting, links limited to http(s) and mailto, and images shown as links.

## 📝 Prompts

The long prompts live in `jarvis/prompts/*.md`, one file each: persona, task brief, goal check, learning review, injection check, skill review...

- **Placeholders** like `{{name}}` are filled by `prompts.load(name, ...)`.
- **Edit them as markdown**; `tests/test_prompts.py` checks each one loads where it's used.

## 🔧 Adding a tool

See **[Integrations → Adding a tool](integrations.md#-adding-a-tool)**.

## 🧰 Editing AI Armory alongside

JARVIS installs AI Armory from GitHub, **pinned to a commit** (`rev` under `[tool.uv.sources]` in `pyproject.toml`), so `uv sync` and `make test` behave the same everywhere. To try changes in a local checkout before pushing them:

**1. Install your checkout** — editable, over the pinned copy in `.venv`:

```sh
uv pip install -e ../ai-armory
```

**2. Keep the pin away** — so `uv run` and `make` don't restore the pinned copy:

```sh
export UV_NO_SYNC=1
```

**3. Restart the services** — they run `.venv`'s Python:

```sh
make services-restart
```

**4. Go back to the pin** — when you're done:

```sh
unset UV_NO_SYNC && uv sync
```

### Moving to new AI Armory work

1. **Push** the AI Armory change.
2. **Set `rev`** in `pyproject.toml` to the new commit.
3. **Run** `uv lock && uv sync`.

---

<div align="center">

[← 📱 Mobile](mobile.md) &nbsp;·&nbsp; [🏠 Home](../README.md) &nbsp;·&nbsp; [🏗️ Architecture →](architecture.md)

</div>
