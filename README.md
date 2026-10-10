<div align="center">

<img src="docs/assets/banner.svg" alt="JARVIS — a personal voice assistant that lives on your Mac" width="100%">

<br>

[![License: MIT](https://img.shields.io/badge/license-MIT-4fd1ff?style=flat-square)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776ab?style=flat-square&logo=python&logoColor=white)](pyproject.toml)
[![macOS · Apple silicon](https://img.shields.io/badge/macOS-Apple%20silicon-111?style=flat-square&logo=apple&logoColor=white)](docs/setup.md)
[![Claude](https://img.shields.io/badge/brain-Claude-d97757?style=flat-square&logo=anthropic&logoColor=white)](docs/architecture.md)
[![LiveKit](https://img.shields.io/badge/voice-LiveKit-1f2937?style=flat-square)](docs/voice.md)

**[Setup](docs/setup.md)** &nbsp;·&nbsp; **[Configuration](docs/configuration.md)** &nbsp;·&nbsp; **[Architecture](docs/architecture.md)** &nbsp;·&nbsp; **[Contributing](CONTRIBUTING.md)**

</div>

---

JARVIS listens, thinks and speaks **on your own Mac**. Speech recognition, speech synthesis, noise filtering and speaker verification all run **on-device**; only the thinking goes to **Claude**. Talk to it from the terminal, a browser, or your **iPhone**. It reads and drafts your mail, runs your calendar, does **code and research tasks in the background** while you keep talking, and tells you what's new **before you ask**.

<table>
<tr>
<td width="33%" valign="top">

### 🔒 Private by default
Whisper, Kokoro, the noise filter and your voiceprint run locally. Data, secrets and logs never leave `data/` and `secrets/`.

</td>
<td width="33%" valign="top">

### 🧠 A real brain
The `claude` CLI with JARVIS's own tools, web search, a sandboxed shell and Google, MCP and Mac integrations.

</td>
<td width="33%" valign="top">

### 🔄 Works while you talk
Background tasks, a heartbeat, a scheduler and a learning review keep going after you hang up.

</td>
</tr>
</table>

## ✨ Features

<table>
<tr>
<td width="50%" valign="top">

**🗣️ Voice**<br>
Whisper large-v3-turbo on the Apple GPU in, Kokoro out. A turn detector decides when you've finished; a speech gate and voiceprint make sure only **you** get answered.

</td>
<td width="50%" valign="top">

**📬 Google**<br>
Gmail, Calendar, Sheets, Docs, Slides, Drive and Chat across several accounts. Sends and deletes wait for your spoken **yes**.

</td>
</tr>
<tr>
<td valign="top">

**🛠️ Background tasks**<br>
Code changes, code questions, research and long shell jobs, in parallel, each checked against its goal before it reports back.

</td>
<td valign="top">

**🔔 Proactive**<br>
A heartbeat for meetings, important mail, PRs and stuck tasks; reminders, recurring jobs and "tell me when…" watches.

</td>
</tr>
<tr>
<td valign="top">

**🔌 Connections**<br>
Finds and connects MCP servers (GitHub, GitLab, Notion, …) from the official registry, with keys in an **encrypted vault**.

</td>
<td valign="top">

**📱 iPhone app**<br>
An installable web app with a control panel, push notifications, typing instead of talking and earphone controls.

</td>
</tr>
<tr>
<td valign="top">

**💻 Mac and web**<br>
Notifications, apps, Shortcuts, volume, screenshots, documents and recordings; a read-only Chrome profile for pages behind your logins.

</td>
<td valign="top">

**🧩 Skills and memory**<br>
Claude Code-format skills written together by voice, plus long-term memory and a to-do list it keeps for you.

</td>
</tr>
</table>

## 🧭 How it fits together

```mermaid
flowchart TB
    You(["📱 iPhone · 💻 browser · ⌨️ terminal"])

    subgraph Mac["🖥️ Your Mac"]
        direction LR
        Web["🌐 Web server<br/>app · control panel"]
        LK["📡 LiveKit<br/>real-time audio"]
        Agent["🎙️ Voice agent<br/>hear → think → speak"]
        Runner["⚙️ Task runner<br/>tasks · heartbeat · scheduler"]
    end

    Claude(["🧠 Claude"])
    World(["🔌 Google · MCP · web"])

    You -->|HTTPS| Web
    You <-->|WebRTC| LK
    LK <--> Agent
    Agent <-->|start · updates| Runner
    Web <--> Runner
    Agent --> Claude
    Runner --> Claude
    Agent --> World
    Runner --> World
    Runner -.->|push| You

    classDef you fill:#e0f2fe,stroke:#0284c7,color:#0c4a6e
    classDef mac fill:#ecfdf5,stroke:#059669,color:#064e3b
    classDef cloud fill:#fff7ed,stroke:#ea580c,color:#7c2d12
    class You you
    class Web,LK,Agent,Runner mac
    class Claude,World cloud
    style Mac fill:#f8fafc,stroke:#94a3b8
```

Four **launchd services** keep it running: LiveKit, the voice agent, the web app and the task runner. The full picture, including one spoken turn step by step, is in **[Architecture](docs/architecture.md)**.

## 🚀 Quickstart

> [!IMPORTANT]
> JARVIS needs **macOS on Apple silicon** (M1 or later, 8 GB is enough), [Homebrew](https://brew.sh), [uv](https://docs.astral.sh/uv/), Node.js and the **Claude Code CLI** with a Claude account.

**1. Install** — LiveKit, Python packages, models and the web app:

```sh
git clone <repo-url> jarvis && cd jarvis
make install
```

**2. Sign in and configure** — JARVIS gets its own Claude login, then `.env`, keys and your passcode:

```sh
make claude-login
make setup            # shows your passcode once: note it down
```

**3. Talk to it** — straight away in the terminal, or as always-on services:

```sh
make console          # talk through the Mac's mic, no server needed
make services-install # or install the four launchd services
open http://localhost:8787
```

> [!TIP]
> Next: Google accounts, the iPhone over Tailscale, projects for code tasks and running with the lid closed are all in **[Setup](docs/setup.md)**.

<details>
<summary><b>Optional extras</b></summary>

<br>

| Extra | Unlocks |
|---|---|
| **Tailscale** | Reaching JARVIS from the iPhone over HTTPS |
| **Google Cloud OAuth client** | Gmail, Calendar, Docs, Sheets, Slides, Drive and Chat |
| **`gh`** | The heartbeat's GitHub checks |
| **`ffmpeg`** | mp3 audio files |
| **Google Chrome** | The web browser tool (Playwright's Chromium otherwise) |
| **claude.ai subscription** | Tasks publishing artifacts |

</details>

## 🧰 Everyday commands

| | Command | What it does |
|:-:|---|---|
| ▶️ | `make console` | Talk in this terminal (no server needed) |
| ▶️ | `make dev` | Run every part in the foreground; <kbd>Ctrl</kbd>+<kbd>C</kbd> stops all |
| 🔁 | `make services-restart` | Rebuild the web app and restart the services |
| 📊 | `make status` · `make logs` | Service health · follow all logs |
| 📋 | `make tasks` | Recent background tasks |
| 🔔 | `make heartbeat` | Dry-run one heartbeat |
| ⏱️ | `make voice-timing` | Where the wait before each reply went |
| 🔑 | `make accounts` · `make servers` · `make vault` | Google accounts · MCP connections · secret names |
| 🧪 | `make test` · `make check` | Unit tests · type-check and import everything |

Run **`make help`** for the full list.

## 📚 Documentation

<table>
<tr>
<td width="50%" valign="top">

**[🚀 Setup](docs/setup.md)**<br>
Install, Claude login, Google, iPhone, projects, running 24/7

**[⚙️ Configuration](docs/configuration.md)**<br>
Every `.env` setting with its default

**[🗣️ Voice](docs/voice.md)**<br>
Turn-taking, noise filter, speech gate, speaker check

**[🛠️ Tasks](docs/tasks.md)**<br>
Background tasks, approvals, scheduler, heartbeat

</td>
<td width="50%" valign="top">

**[🔌 Integrations](docs/integrations.md)**<br>
Google, MCP connections, browser, files, skills

**[📱 iPhone](docs/iphone.md)**<br>
The web app, notifications, the native app

**[👩‍💻 Development](docs/development.md)**<br>
Code map, tests, web app, adding a tool

**[🏗️ Architecture](docs/architecture.md)**<br>
The deep dive: components, data, security

</td>
</tr>
</table>

## 🔐 Security model

JARVIS acts on your behalf, so what it may do alone is deliberately narrow. Every guard is **enforced in code**, not left to the model.

| | Guard | What it means |
|:-:|---|---|
| 🗣️ | **Spoken yes** | Emails, messages, invites and deletions are drafted, read aloud and sent only after a clear yes |
| ✋ | **Approvals** | Deleting, pushing, installing, killing processes or sending data off the Mac waits for you |
| 📦 | **Sandboxes** | The voice shell runs in Claude Code's sandbox; tasks run under a `sandbox-exec` profile that hides secrets |
| 🔌 | **Gated connections** | MCP tools that change things need a yes; unsorted tools are refused |
| 🔒 | **Encrypted vault** | Tokens and keys are AES-256-GCM encrypted; keys are pasted, never spoken |
| 🛡️ | **Injection checks** | Mail, pages, documents and tool results are screened for hidden instructions |
| 🏠 | **Local only** | The web app binds to 127.0.0.1; the iPhone gets in over Tailscale with a per-person passcode |

> [!NOTE]
> `.env`, `data/` and `secrets/` stay on your Mac and are git-ignored. What you say, and what tools read, goes to Claude. Known limits are listed in **[Architecture](docs/architecture.md)**.

## 🙏 Built with

[Claude Code](https://docs.anthropic.com/en/docs/claude-code) · [LiveKit Agents](https://github.com/livekit/agents) · [MLX Whisper](https://github.com/ml-explore/mlx-examples) · [Kokoro](https://github.com/thewh1teagle/kokoro-onnx) · [Silero VAD](https://github.com/snakers4/silero-vad) · [DeepFilterNet](https://github.com/Rikorose/DeepFilterNet) · [WeSpeaker](https://github.com/wenet-e2e/wespeaker) · [Playwright](https://playwright.dev) · [Preact](https://preactjs.com) · [AI Armory](https://github.com/anilkumarmeena/ai-armory)

## 🤝 Contributing

Contributions are welcome. **[CONTRIBUTING.md](CONTRIBUTING.md)** covers the dev setup, tests, code style and what makes a good pull request.

## 📄 License

Released under the **[MIT License](LICENSE)**.

<div align="center">
<br>
<img src="docs/assets/logo.svg" width="48" alt="">
<br>
<sub>Made for talking, not typing.</sub>
</div>
