<div align="center">

<img src="assets/logo.svg" width="72" alt="">

# 👩‍💻 Development

**How the code is laid out, how to test it, and how to work on the web app and AI Armory alongside it.**

[🏠 Home](../README.md) · [🚀 Setup](setup.md) · [⚙️ Configuration](configuration.md) · [🗣️ Voice](voice.md) · [🛠️ Tasks](tasks.md) · [🔌 Integrations](integrations.md) · [📱 iPhone](iphone.md) · **👩‍💻 Development** · [🏗️ Architecture](architecture.md)

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
├── tools/           what the brain can call: Google, Mac, files, memory, to-dos, tasks,
│                    schedules, browser, skills
├── tasks/           the task runner, its worker and client, projects, heartbeat, scheduler,
│                    learning review
├── safety/          command rules, the Mac sandbox, approvals (and remembered ones), consent,
│                    the gates, hidden-instruction scan and injection check
├── integrations/    Google accounts, MCP servers (registry, launcher, relay, OAuth),
│                    the web browser, sign-ins
├── skills/          skills: storage, installing, script reviews
├── notify/          Web Push and APNs
├── state/           memory, settings, to-dos, vocabulary, voiceprints, history, pictures
├── core/            config, database, vault, credentials, logins, profiles, documents,
│                    transcription
├── server.py        web app, token endpoint and push registration
├── panel.py         the control panel's actions
└── setup_local.py   local setup and the launchd services

web/                 the phone web app (Vite + TypeScript + Preact), served from web/dist
tests/               pytest, plus web/tests/ (vitest)
deploy/              launchd plists
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

- **`VoiceSession`** routes each turn and speaks tasks' requests.
- **`ask_json`** (in the brain) is the one way a short model call gives a verdict: JSON checked against a schema.
- **The guards:** `commands.py` (what's risky), `shell.py` / `draft_gate.py` (spoken yes), `host.py` (sandbox profile), `injection.py` / `scan.py`.
- **The Google and Mac tools** come from **[AI Armory](https://github.com/anilkumarmeena/ai-armory)**, a git dependency pinned to a commit in `pyproject.toml`, registered in `jarvis/tools/google.py` and `jarvis/tools/mac.py`.

## 🧪 Tests and checks

| | Command | Runs |
|:-:|---|---|
| 🧪 | `make test` | The pytest suite in `tests/`, plus the web app's vitest tests |
| 🌐 | `npm test --prefix web` | The web app's tests alone |
| ✅ | `make check` | Byte-compiles `jarvis/`, **pyright**, web type-check, imports every entry module |
| 🧱 | `make host-check` | The real sandbox walls around tasks' commands |
| 🖼️ | `make artifact-check` | Publishes a tiny claude.ai artifact on JARVIS's login, the way tasks do |

- **`make test`** fetches pytest with `uv run --with pytest`. The vitest tests are skipped if `web/node_modules` is missing (`make web-install`).
- **Pyright** is a pinned version, errors only. Its softer rules are listed under `[tool.pyright]` in `pyproject.toml`; `uv run --with pyright pyright` shows their warnings.
- **`make host-check`** checks that secrets and JARVIS's files are blocked, while PyPI, the task cache, the SSH agent and code signing stay reachable.
- **`make artifact-check`** takes `FILE=page.html` to publish a real page.

> [!NOTE]
> Tests use a **scratch data folder** (`tests/conftest.py` sets `JARVIS_DATA_DIR` and `JARVIS_DB_FILE`), so they never touch your data. The voice brain's tests run against a **fake `claude` CLI** (`tests/fake_cli.py`).

## 🏃 Running the parts

| | Command | Runs |
|:-:|---|---|
| 🚀 | `make dev` | LiveKit, web app, agent (auto-reload) and runner together in the foreground |
| 🧩 | `make agent` · `make runner` · `make web` · `make livekit` | One part on its own |
| ⌨️ | `make console` | The agent in the terminal, no LiveKit |

> [!IMPORTANT]
> Stop the launchd services first (`make services-stop`) so ports and the runner's socket are free.

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

- **Keys** wired in `main.tsx`: <kbd>Space</kbd>, <kbd>M</kbd>, <kbd>S</kbd>, <kbd>C</kbd>, <kbd>Esc</kbd>.
- **`store.ts`** is mirrored onto `body[data-*]` for CSS.
- **`mic.ts`** handles earphones and mic recovery; **`markdown.ts`** uses marked + DOMPurify.

| | Task | How |
|:-:|---|---|
| 📦 | **Build** | `make web-build` → `web/dist` |
| 🔥 | **Hot reload** | `make web` (or the services) for the API, plus `make web-dev` |
| 🏷️ | **Build ids** | Each build carries an id; an older page reloads itself between sessions |

- **Builds:** `make web`, `services-install` and `services-restart` build first; a running server picks up a rebuild without restarting.
- **Hot reload:** http://localhost:5173 serves `web/src` and proxies `/api` to 8787.
- **Build ids** live in `<meta name="jarvis-build">`; the server reports the one it serves.

> [!WARNING]
> **Untrusted text in the panel.** Task summaries, memory and to-dos can come from an email or page a task read. So Markdown is rendered with raw HTML shown as text, DOMPurify keeping only plain formatting, links limited to http(s) and mailto, and images shown as links.

## 📝 Prompts

The long prompts live in `jarvis/prompts/*.md`, one file each: persona, task brief, goal check, learning review, heartbeat triage, injection check, skill review...

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

[← 📱 iPhone](iphone.md) &nbsp;·&nbsp; [🏠 Home](../README.md) &nbsp;·&nbsp; [🏗️ Architecture →](architecture.md)

</div>
