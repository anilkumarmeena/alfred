<div align="center">

<img src="assets/logo.svg" width="72" alt="">

# ⚙️ Configuration

**Every setting Alfred reads, grouped by area, with its default, and where each one can be set.**

[🏠 Home](../README.md) · [🚀 Setup](setup.md) · **⚙️ Configuration** · [🗣️ Voice](voice.md) · [🛠️ Tasks](tasks.md) · [🔌 Integrations](integrations.md) · [📱 Mobile](mobile.md) · [👩‍💻 Development](development.md) · [🏗️ Architecture](architecture.md) · [🧭 Features](features.md)

</div>

---

**Nothing here is required**: `make setup` writes the few secrets Alfred needs, and everything else has a sensible default.

> [!IMPORTANT]
> **How settings work.** Each setting is read from four places, **and the later one wins**:
>
> | # | Where | What it's for |
> |:-:|---|---|
> | 1 | `config/alfred.toml`, `config/models.toml` in the checkout | The **shipped defaults**. Don't edit them: they change with updates |
> | 2 | `config.toml` in **ALFRED_HOME** | **Your overrides**, same tables and keys (e.g. `[tasks]` `max_turns = 200`) |
> | 3 | `.env` in **ALFRED_HOME** (copied from `.env.example`, mode 600) | Secrets and quick overrides: the variable named in each table below |
> | 4 | The app's **Settings** ([below](#-changed-in-the-app)) | A few preferences. When the variable is set in `.env`, **it wins** and the app only shows it |
>
> - **Empty = default:** a setting that is present but empty counts as unset.
> - **On/off values:** `1` `on` `true` `yes` · `0` `off` `false` `no`.
> - **Apply a change** with `make services-restart`.

> [!TIP]
> **Models are picked by role, not by name.** `config/models.toml` maps each job (the voice, a task, an announcement…) to a tier, and each tier to a model: **deep** `claude-opus-5-5`, **balanced** `claude-sonnet-5`, **fast** `claude-haiku-5-5`, written below as **Opus**, **Sonnet** and **Haiku**. Change a tier for every job that uses it in your `config.toml` (`[models.tiers]` `fast = "…"`), or one job with its variable below.

### 📂 ALFRED_HOME

Everything an install keeps lives in one folder, **ALFRED_HOME**: `config.toml`, `.env`, `data/` (the database, task worktrees, caches), `secrets/`, `models/` and `logs/`. It is:

1. the `ALFRED_HOME` variable, if set;
2. else **`~/.alfred`**, once it holds an install (a `.env` or `data/`), so a lone `config.toml` there never swaps in an empty one;
3. else the checkout itself (the layout of installs made before ALFRED_HOME).

`make home-move` moves an existing install's files out of the checkout into `~/.alfred` (or `ALFRED_HOME`), then rewrites the services so they find them, so the checkout holds only code and `git clean` can't touch your data. A custom `ALFRED_HOME` gets a `~/.alfred` link, so later `make` commands find it without the variable. Until you move it, your `config.toml` goes in the checkout. `ALFRED_CONFIG` names a different settings file than ALFRED_HOME's `config.toml`.

<details>
<summary><b>📑 On this page</b></summary>

<br>

| | Group | Covers |
|:-:|---|---|
| 📱 | [Changed in the app](#-changed-in-the-app) | Settings you can also set from the control panel |
| 👤 | [You, access and services](#-you-access-and-services) | Name, time zone, ports, data folder |
| 🧠 | [Brain](#-brain) | The voice brain's models, effort and tools |
| 🔒 | [Safety](#-safety) | Approvals, consent, injection checks |
| 🎤 | [Speech in and out](#-speech-in-and-out) | Whisper (MLX or faster-whisper), Kokoro, turn-taking, noise filter, memory, audio files |
| 🔇 | [Speech gate](#-speech-gate) | Dropping phantom clips |
| 👂 | [Speaker check](#-speaker-check) | Answering only your voice |
| 🧰 | [Background tasks](#-background-tasks) | Task limits, models, goal loop |
| 🌐 | [Web browser](#-web-browser) | The browser tools |
| 🔔 | [Notices, scheduler and learning](#-notices-scheduler-and-learning) | Alfred's own notices and scheduled work |
| 🔔 | [Notifications](#-notifications) | Web Push and the native app |

</details>

## 📱 Changed in the app

A few settings live in the settings table of `data/tasks.db` and are changed in the control panel: **Settings → About you**, **Settings → Do not disturb** for quiet hours, or **Settings → Behaviour** and **Settings → Safety check** for the rest.

| | In the app | Variables | Applies |
|:-:|---|---|---|
| 👤 | **About you**: your assistant's name, your name, time zone | `ALFRED_ASSISTANT_NAME`<br>`ALFRED_USER_NAME`<br>`ALFRED_TIMEZONE` | Next conversation or task, no restart |
| 🗣️ | **Voice**: voice, speed, phantom filter | `ALFRED_TTS_VOICE`<br>`ALFRED_TTS_SPEED`<br>`ALFRED_SPEECH_GATE` | Next conversation |
| 👂 | **Voice ID**: other voices, how sure it must be | `ALFRED_SPEAKER_CHECK`<br>`ALFRED_SPEAKER_THRESHOLD` | Next conversation |
| 🧰 | **Background work**: tasks at once, web browser, browser window | `ALFRED_MAX_TASKS`<br>`ALFRED_BROWSER`<br>`ALFRED_BROWSER_WINDOW` | Task limit: runner restart<br>Browser: next conversation |
| 🔒 | **Safety**: hidden instruction check, checked by | `ALFRED_INJECTION_CHECK`<br>`ALFRED_INJECTION_MODEL` | Next conversation or task |
| 🌙 | **Do not disturb**: nightly quiet hours, local time (`23:00-08:00`; `off`: never quiet). Watches pause and notices wait | `ALFRED_QUIET_HOURS` | At once |

On Alfred's computer, the same preferences are shown and changed from a terminal:

```sh
uv run python -m alfred.state.settings prefs                 # show them
uv run python -m alfred.state.settings prefs voice bm_lewis  # change one
```

## 👤 You, access and services

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_ASSISTANT_NAME` | `Alfred` | **The name your assistant goes by**: what it calls itself, answers to and signs its notifications with, and how the app shows it. Up to 24 characters, starting with a letter. Each profile names its own |
| `ALFRED_USER_NAME` | Your user account's first name | What Alfred calls you in prompts, logs and task instructions |
| `ALFRED_TIMEZONE` | The computer's, else `UTC` | Your time zone, e.g. `Europe/London` |
| `ALFRED_PASSCODE` | Made by setup | **First setup only**: becomes your login, then is taken out of `.env` |
| `LIVEKIT_API_KEY`<br>`LIVEKIT_API_SECRET` | Made by setup | Moved into the vault on first start |
| `LIVEKIT_URL` | `ws://127.0.0.1:7880` | The local LiveKit server |
| `ALFRED_WEB_PORT` | `8787` | The web app's port (on 127.0.0.1) |
| `ALFRED_SERVICE_PREFIX` | `com.alfred` | Start of the service names (below); change only to run a second Alfred |
| `ALFRED_DATA_DIR`<br>`ALFRED_DB_FILE` | `data/`<br>`data/tasks.db` | Where the data folder and database live |
| `GOOGLE_CLIENT_SECRET` | `secrets/client_secret*.json` | A Google OAuth client file, until one is pasted into the vault |

- **`ALFRED_PASSCODE`** is stored hashed in `secrets/logins.json`.
- **`ALFRED_TIMEZONE`** is read from `/etc/localtime`; on Linux, failing that, `/etc/timezone`, `timedatectl` or `TZ`.
- **`ALFRED_SERVICE_PREFIX`** is for a second Alfred under the **same user account**. Each service is `<prefix>.<name>` (`livekit`, `agent`, `web`, `tasks`):

| | macOS | Linux |
|---|---|---|
| **Service manager** | launchd | systemd, **user** units |
| **`com.alfred.agent` is** | The launchd label | The unit `com.alfred.agent.service` in `~/.config/systemd/user/` |

## 🧠 Brain

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_CLAUDE_CLI` | Bundled with `claude-agent-sdk` | Path to a `claude` binary |
| `ALFRED_CLAUDE_CONFIG_DIR` | Empty: your Claude Code login | A Claude login of Alfred's own, e.g. `secrets/claude` (then `make claude-login`) |
| `ALFRED_MASTER_KEY` | Made by setup | Encrypts the vault; `make vault ARGS=init` makes one |
| `ALFRED_CLI_MODEL` | Opus | The voice brain's model |
| `ALFRED_EFFORT` | `low` | Its effort; low keeps spoken turns quick |
| `ALFRED_MAX_TOOL_ROUNDS` | `8` | Most model turns in one reply |
| `ALFRED_TURN_TOOLS`<br>`ALFRED_TURN_SECONDS` | `3`<br>`15` | How much work a spoken reply does itself: past this many calls or seconds, more are refused and the brain hands the rest to a lookup or a task |
| `ALFRED_VOICE_QUICK_MODEL` | *(off)* | A lighter model (e.g. `claude-sonnet-5`) for simple turns |
| `ALFRED_LOOKUP_MODEL`<br>`ALFRED_LOOKUP_WAIT` | Haiku<br>`8` | The voice brain's read-only file and computer lookups: model, and seconds a spoken turn waits on them before they carry on in the background |
| `ALFRED_SIDE_MODEL`<br>`ALFRED_SIDE_MINUTES` | Sonnet<br>`3` | Background lookups that let the conversation carry on: model and time limit (past it, one carries on as a task) |
| `ALFRED_PROACTIVE_JUDGE` | `on` | A fast model picks the moment to bring up task updates and alerts |
| `ALFRED_TOOL_SEARCH` | `true` | Connections' tools load on demand via tool search, and in background tasks Alfred's own account tools too |
| `ALFRED_CONNECTION_WAIT` | `20` | Seconds a connection's call may hold up a spoken reply |
| `ALFRED_TOOL_WAIT` | `8` | Seconds one of Alfred's own tools (a merge, a long read) may hold up a spoken reply |

- **`ALFRED_VOICE_QUICK_MODEL`** is used for turns judged to need no tools and little thought.
- **`ALFRED_PROACTIVE_JUDGE=off`** says each update once nobody is talking.
- **`ALFRED_TOOL_SEARCH`**: `auto:N` loads tools on demand once they'd fill N% of context; `false` loads all up front.
- **`ALFRED_CONNECTION_WAIT`**: past it, the call carries on as a background task; `0` always waits.
- **`ALFRED_TOOL_WAIT`**: past it, the call carries on in the background and Alfred follows up on its result by itself; `0` always waits.

> [!WARNING]
> **Keep `.env` and `data/tasks.db` apart.** `ALFRED_MASTER_KEY` in `.env` decrypts the vault inside `data/tasks.db`; anyone holding both holds your secrets.

## 🔒 Safety

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_APPROVAL_MINUTES` | `5` | How long a spoken yes/no question stays open, and a yes stays good |
| `ALFRED_TASK_APPROVAL_HOURS` | `12` | How long a background task's request to run a risky command waits |
| `ALFRED_CONSENT` | `on` | Unclear replies go to a short model call (below) |
| `ALFRED_CONSENT_MODEL`<br>`ALFRED_CONSENT_SECONDS` | Haiku<br>`8` | Its model and time limit |
| `ALFRED_INJECTION_CHECK` | `on` | Screen every tool result for hidden instructions before the model acts on it |
| `ALFRED_INJECTION_MODEL` | Haiku | The model that screens (the app offers Haiku or Sonnet) |
| `ALFRED_INJECTION_VOICE_SECONDS`<br>`ALFRED_INJECTION_TASK_SECONDS` | `6`<br>`30` | A slower check passes the result on, marked unchecked |

- **Consent:** a reply the yes/no word lists can't read goes to a model call that sees **only your words and what's waiting**. Past its time limit, the reply counts as unclear and you're asked again.

## 🎤 Speech in and out

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_STT` | Per backend (below) | Whisper model for live voice |
| `ALFRED_STT_DEVICE` | *(auto)* | faster-whisper only: `cuda` or `cpu`; empty uses an NVIDIA GPU when one works |
| `ALFRED_TRANSCRIBE_MODEL` | Per backend (below) | Whisper model for [recordings](integrations.md#-recordings), which can be in any language |
| `ALFRED_STT_PROMPT` | `on` | Give Whisper the speech vocabulary and project names as its prompt |
| `ALFRED_TTS_VOICE` | `bm_george` | Kokoro voice: `bm_*`/`bf_*` British, `am_*`/`af_*` American |
| `ALFRED_TTS_SPEED` | `1.05` | Speaking speed, 0.5 to 2.0 |
| `ALFRED_TTS_MODEL` | `kokoro-v1.0.onnx` | Kokoro model in `models/` |
| `ALFRED_TTS_THREADS` | One fewer than performance cores (every core counts on Linux), 2–4 | Kokoro's ONNX Runtime threads; `0` uses every core |
| `ALFRED_TURN_SILENCE` | `0.8` | Seconds after you stop speaking before your turn ends (up to 5; never sooner than `ALFRED_VAD_MIN_SILENCE`); it also waits for Whisper's words |
| `ALFRED_VAD_MIN_SILENCE` | `0.55` | Silence that ends an utterance; the floor under every reply's wait |
| `ALFRED_VAD_MIN_SPEECH`<br>`ALFRED_VAD_THRESHOLD` | `0.2`<br>`0.5` | Unbroken speech before an utterance starts; probability that counts as speech |
| `ALFRED_WHISPER_UNSURE_LOGPROB` | `-1.0` | Below this average log-probability a turn is marked unsure, so Alfred checks back |
| `ALFRED_DENOISE` | `on` | Noise filter: `on` everywhere, `vad` only for the VAD, `off` |
| `ALFRED_DENOISE_ATTEN_DB` | `18` | Most the filter takes off any sound, in dB (`0` no limit) |

**Whisper defaults** depend on the machine ([how each runs](voice.md#-whisper-in-kokoro-out)):

| Backend | `ALFRED_STT` (live voice) | `ALFRED_TRANSCRIBE_MODEL` (recordings) |
|---|---|---|
| **MLX**, Apple silicon | `mlx-community/whisper-large-v3-turbo-q4` | Same as `ALFRED_STT` |
| **faster-whisper**, NVIDIA GPU | `large-v3-turbo` | `large-v3-turbo` |
| **faster-whisper**, CPU | `small.en` | `small` (multilingual) |

- **Whisper names:** MLX takes a Hugging Face repo; faster-whisper a size (`base.en`, `small.en`, `large-v3-turbo`...) or a CTranslate2 folder. An `mlx-community/` model elsewhere falls back to the default.
- **Whisper sizes:** on MLX the 4-bit default is ~0.5 GB, the 16-bit original ~1.6 GB. On a slow CPU, `ALFRED_STT=base.en` answers quicker.
- **Kokoro 8-bit:** `kokoro-v1.0.int8.onnx` saves ~230 MB but renders 30–60% slower. `make models-int8` downloads it.
- **Turn delays:** the min applies when you sound finished, the max at most mid-thought. Each speaker's delays then follow their pace.

<details>
<summary><b>💾 Memory use</b> · 5 settings</summary>

<br>

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_VOICE_IDLE_PROCESSES` | `1` | Warm spare voice processes (~300 MB each) so a call connects at once |
| `ALFRED_MLX_CACHE_MB` | `256` | **Apple silicon only.** GPU memory MLX keeps for reuse after Whisper; `0` frees after every clip, `-1` no cap |
| `ALFRED_TTS_MEMORY_ARENA` | `off` | Kokoro's ONNX memory arena (grows to ~1 GB after a long sentence) |
| `ALFRED_VOICE_MALLOC_LARGE_CACHE` | `off` | Let the allocator keep large freed blocks in voice processes (macOS's large cache, glibc's growing mmap threshold) |
| `ALFRED_VOICE_MEMORY_LOG_MINUTES` | `10` | How often `logs/agent.log` gets a `voice memory` line (`0` never) |

`uv run python -m alfred.voice.ram` prints the voice memory line on demand.

</details>

<details>
<summary><b>🎧 Audio files</b> · 4 settings</summary>

<br>

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_AUDIO_DIR` | Your download folder | Where [audio files](integrations.md#-audio-files) go; Alfred's own folder if it can't write there |
| `ALFRED_AUDIO_FORMAT` | `m4a` | `m4a` (Linux: needs `ffmpeg`), `mp3` (needs `ffmpeg` or `lame`) or `wav` |
| `ALFRED_AUDIO_MODEL` | `kokoro-v1.0.onnx` | The Kokoro model audio files are read with |
| `ALFRED_AUDIO_THREADS` | A quarter of the cores, at least 2 | CPU threads for rendering, leaving the rest to the live voice |

The download folder is `~/Downloads` on macOS, and on Linux the XDG one from `~/.config/user-dirs.dirs` (maybe translated, like `~/Téléchargements`).

</details>

## 🔇 Speech gate

Drops clips with no real speech before the brain hears them. What each check means is in **[🗣️ Voice](voice.md#-speech-gate)**.

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_SPEECH_GATE` | `enforce` | `enforce` drops phantom clips, `shadow` only logs them, `off` checks nothing |

<details>
<summary><b>🎚️ Fine-tuning</b> · 19 settings</summary>

<br>

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_SPEECH_GATE_MIN_SPEECH_SECONDS` | `0.1` | **No speech**: less VAD speech than this |
| `ALFRED_SPEECH_GATE_MIN_SECONDS` | `0.3` | **Too short**: clip shorter than this |
| `ALFRED_SPEECH_GATE_MIN_DBFS` | `-55` | **Too quiet**: loudest 100 ms below this |
| `ALFRED_SPEECH_GATE_NO_SPEECH` | `0.8` | **Whisper hears nothing**: no-speech probability at least this… |
| `ALFRED_SPEECH_GATE_WEAK_SPEECH_SECONDS` | `0.25` | …and less VAD speech than this |
| `ALFRED_SPEECH_GATE_FAINT_SPEECH_SECONDS` | `0.25` | **Faint**: less speech than this… |
| `ALFRED_SPEECH_GATE_FAINT_DBFS` | `-28` | …and quieter than this… |
| `ALFRED_SPEECH_GATE_FAINT_MAX_SECONDS` | `1.4` | …and shorter than this |
| `ALFRED_SPEECH_GATE_PHANTOM_DBFS` | `-22` | **Phantom phrase** bar: a known phantom must be this loud… |
| `ALFRED_SPEECH_GATE_PHANTOM_SPEECH_SECONDS` | `0.6` | …and have this much speech |
| `ALFRED_SPEECH_GATE_PHANTOMS` | Built-in list | Comma-separated phrases that replace the phantom list |
| `ALFRED_SPEECH_GATE_MIN_LOGPROB` | `-1.0` | **Whisper unsure**: average log-probability under this… |
| `ALFRED_SPEECH_GATE_UNSURE_SPEECH_SECONDS` | `0.5` | …and less speech than this |
| `ALFRED_SPEECH_GATE_MAX_COMPRESSION` | `2.4` | **Repetitive**: gzip compression ratio over this |
| `ALFRED_SPEECH_GATE_SAVE_AUDIO` | On in `shadow`, off otherwise | Save each judged clip as a WAV in `logs/speech-gate-audio/` |
| `ALFRED_SPEECH_GATE_SAVE_SHORT` | `on` | Still save short clips it lets through… |
| `ALFRED_SPEECH_GATE_SHORT_SECONDS` | `2` | …under this long… |
| `ALFRED_SPEECH_GATE_SHORT_SPEECH_SECONDS` | `0.5` | …or with less speech than this |
| `ALFRED_SPEECH_GATE_AUDIO_KEEP` | `300` | Newest saved clips kept |

</details>

## 👂 Speaker check

Answers only your enrolled voice. What each case means is in **[🗣️ Voice](voice.md#-speaker-check)**.

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_SPEAKER_CHECK` | `on` | `on` drops speech that isn't yours, `shadow` only logs, `off` checks nothing |
| `ALFRED_SPEAKER_THRESHOLD` | `0.35` | Cosine similarity a clip with 3 s+ of speech needs (0.1–0.8) |
| `ALFRED_SPEAKER_BARGE_IN` | `on` | Only your voice interrupts Alfred |

- **No voiceprint, no check**, whatever `ALFRED_SPEAKER_CHECK` says.
- **Threshold in the app:** Lenient 0.25, Normal (0.35), Strict 0.45.

<details>
<summary><b>🎚️ Fine-tuning</b> · 15 settings</summary>

<br>

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_SPEAKER_SHORT_MARGIN` | `0.1` | Short clips need this much less… |
| `ALFRED_SPEAKER_SHORT_SECONDS` | `1` | …at this length or under… |
| `ALFRED_SPEAKER_LONG_SECONDS` | `3` | …sliding back up to the threshold by this length |
| `ALFRED_SPEAKER_RELAX` | `0.1` | Extra leniency mid-conversation… |
| `ALFRED_SPEAKER_RECENT_SECONDS` | `30` | …when you were heard within this long… |
| `ALFRED_SPEAKER_REPLY_SECONDS` | `10` | …or Alfred finished speaking within this long |
| `ALFRED_SPEAKER_MIN_SECONDS` | `0.4` | Mid-conversation, a clip under this passes as too short to tell |
| `ALFRED_SPEAKER_ADAPT`<br>`ALFRED_SPEAKER_ADAPT_MARGIN` | `on`<br>`0.1` | Tune the reference to this call's mic and room from clearly recognised speech |
| `ALFRED_SPEAKER_ENROLL_SECONDS` | `25` | Enrolling by voice: speech collected |
| `ALFRED_SPEAKER_ENROLL_MIN_SECONDS` | `12` | …least accepted |
| `ALFRED_SPEAKER_ENROLL_TIMEOUT` | `120` | …how long it waits |
| `ALFRED_SPEAKER_BARGE_IN_MIN_SECONDS` | `0.5` | Speech before the first barge-in check |
| `ALFRED_SPEAKER_BARGE_IN_STEP` | `0.25` | Speech between barge-in checks |
| `ALFRED_SPEAKER_BARGE_IN_MAX_SECONDS` | `1` | Most speech judged per barge-in check |

</details>

## 🧰 Background tasks

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_MAX_TASKS` | `6` | Tasks running at once (1–12); the rest queue |
| `ALFRED_RESUME_TASKS` | `3` | Tasks cut off by a runner restart that resume by themselves (0–12) |
| `ALFRED_TASK_MODEL`<br>`ALFRED_TASK_EFFORT` | Opus<br>`high` | Model and effort for background tasks |
| `ALFRED_TASK_MAX_TURNS` | `150` | Most model turns in one task run |
| `ALFRED_TASK_CONTINUES` | `3` | Times a task that runs out of turns carries on by itself before it stops and says so (0–20, `0` never) |
| `ALFRED_TASK_SEARCH_MODEL` | Haiku | A task's read-only code-search subagent |
| `ALFRED_TASK_HELPER_MODEL` | Sonnet | A task's test-runner and web-research subagents |
| `ALFRED_NEW_PROJECTS_DIR` | Folder this checkout is in | Where a project made by voice gets its folder |
| `ALFRED_TASK_READ_DIRS` | Same as above | Comma-separated folders code tasks may read but not change |
| `ALFRED_TASK_ARTIFACTS` | `on` | Let tasks publish mockups and previews as claude.ai artifacts |
| `ALFRED_TASK_STALL_MINUTES` | `15` | Flag a running task with no progress for this long (`0` off) |
| `ALFRED_GOAL_LOOP` | `on` | Check a finished task against what it was asked, and send it back to finish |
| `ALFRED_GOAL_MODEL`<br>`ALFRED_GOAL_ROUNDS` | Task model<br>`2` | The check's model, and send-backs per request (0–5) |
| `ALFRED_GOAL_MAX_COST` | `0` (no limit) | Stop sending back past this many CLI-reported dollars |
| `ALFRED_GOAL_DAILY_CHECKS` | `100` | Most goal checks per 24 hours |
| `ALFRED_ANNOUNCE`<br>`ALFRED_ANNOUNCE_MODEL`<br>`ALFRED_ANNOUNCE_SECONDS` | `on`<br>Haiku<br>`30` | Word a task's end, and a background lookup's answer that isn't already short plain speech, with a short model call; else fixed wording, or the answer trimmed to plain sentences |

- **Projects folder:** if this checkout sits directly in your home folder, the default is `~/Projects`.
- **Read-only folders:** secrets inside stay hidden; set but empty means none.
- **Artifacts** need a claude.ai **subscription** login, not an API key.

## 🌐 Web browser

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_BROWSER` | `on` | Give the voice brain the [web browser](integrations.md#-web-browser) tools |
| `ALFRED_BROWSER_WINDOW` | `headed` | `headed` shows its window (to watch and log in); `headless` hides it |

## 🔔 Notices, scheduler and learning

### Notices

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_NOTICES` | `on` | Alfred's [notices](tasks.md#-notices) about itself: stuck or waiting tasks, usage limits, failing connection sign-ins |

Quiet hours are a [preference](#-changed-in-the-app) (`ALFRED_QUIET_HOURS`, set in **Settings → Do not disturb**). To turn them off, set `off`: like any preference, an empty value means the default.

### Scheduler

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_SCHEDULER` | `on` | The [scheduler](tasks.md#-scheduler); jobs are kept while off |
| `ALFRED_SCHEDULE_MODEL`<br>`ALFRED_SCHEDULE_EFFORT` | Sonnet<br>`medium` | Scheduled tasks and watch checks (scheduled code changes use the task model) |
| `ALFRED_VERDICT_MODEL` | Haiku | Reads a finished run's yes/no: watch met, nothing new, rerun wanted |
| `ALFRED_COMMAND_SUMMARY_MODEL` | Haiku | Sums up a successful command task |
| `ALFRED_COMMAND_TRIAGE_MODEL` | Sonnet | Reads a failed command task and decides whether a task fixes it |
| `ALFRED_USAGE_HOLD` | `0.9` | Share of Claude's five-hour window used before scheduled work waits for the reset (0.5–1) |

### Learning and skills

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_LEARNING` | `on` | The [learning review](tasks.md#-learning-review) |
| `ALFRED_LEARNING_MODEL` | Haiku | Its one call per conversation |
| `ALFRED_LEARNING_QUIET_MINUTES` | `10` | Quiet minutes after which a conversation counts as over |
| `ALFRED_SKILL_REVIEW_MODEL` | Sonnet | Reviews an installed [skill](integrations.md#-skills)'s scripts |
| `ALFRED_TODO_MATCH`<br>`ALFRED_TODO_MATCH_MODEL`<br>`ALFRED_TODO_MATCH_SECONDS` | `on`<br>Haiku<br>`8` | Judge whether a new to-do repeats one [on the list](tasks.md#-to-do-list) that shares most of its words; off, or on failure, only the same wording counts |

## 🔔 Notifications

| Setting | Default | What it does |
|---|---|---|
| `ALFRED_PUSH` | `on` | Send alerts and task updates to subscribed phones |
| `ALFRED_PUSH_CONTACT` | Default Google account's address | `mailto:` or `https:` contact push services may use |
| `ALFRED_APNS_TEAM_ID` | *(none)* | Apple developer team id, for the [native app](mobile.md#-native-app) |
| `ALFRED_APNS_KEY_ID` | *(none)* | The APNs key id; moved into the vault on start |

- **`ALFRED_APNS_KEY_ID`** can also be entered in **Settings → Logins and keys**.
- **Android app push** needs no setting: save the Firebase service-account key as `secrets/fcm_service_account.json` ([how](mobile.md#android-push-on-the-computer)).

---

<div align="center">

[← 🚀 Setup](setup.md) &nbsp;·&nbsp; [🏠 Home](../README.md) &nbsp;·&nbsp; [🗣️ Voice →](voice.md)

</div>
