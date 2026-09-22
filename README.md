<div align="center">

# JARVIS

**A personal voice assistant that runs 24/7 on your MacBook.**

Talk to it from the Mac or the iPhone. Everything runs locally except Claude itself,<br/>which is reached through the local `claude` CLI.

[Architecture](docs/architecture.md) · [Setup](#setup) · [Settings](#settings-env) · [Commands](#everyday-commands) · `make help`

</div>

---

## Setup

| Step | What you get |
|---|---|
| [1. Claude login](#1-claude-login-for-jarvis) | JARVIS's own Claude account, apart from your Claude Code |
| [2. Local config](#2-local-config) | `.env`, LiveKit keys, the phone passcode |
| [3. Try it on the Mac](#3-try-it-on-the-mac) | Talking in the terminal or the browser |
| [4. Google](#4-google) | Gmail, Calendar, Sheets, Docs, Slides, Drive, Chat |
| [5. iPhone via Tailscale](#5-iphone-via-tailscale) | The home-screen app on your phone |
| [6. Projects for code tasks](#6-projects-for-code-tasks) | Repos background tasks can work on |
| [7. Run it 24/7](#7-run-it-247) | Four launchd services that never stop |

### 1. Claude login for JARVIS

Separate from your everyday Claude Code. From the repo's folder:

```sh
CLAUDE_CONFIG_DIR="$PWD/secrets/claude" claude auth login --email <account email>
CLAUDE_CONFIG_DIR="$PWD/secrets/claude" claude auth status
```

`make claude-login` and `make claude-status` do the same.

### 2. Local config

```sh
uv run python -m jarvis.setup_local
```

This copies `.env.example` to `.env` if there's none yet, generates the LiveKit keys, a 6-digit phone passcode and the vault's master key into it, and writes `secrets/livekit.yaml`. It then moves the LiveKit keys into the vault and makes the passcode your login (kept only as a hash in `secrets/logins.json`), both out of `.env`, and shows the passcode once: note it down. Later changes, and logins for other people, are in the control panel: Settings → Logins and keys. Then:

- Fill in who you are in `.env`: your name and time zone (see [Settings](#settings-env)).
- Tell JARVIS what it should know about you (see [About you](#about-you)).

> [!NOTE]
> `.env` and everything in `data/` and `secrets/` stay on your Mac.

### 3. Try it on the Mac

- **In the terminal:** `uv run python -m jarvis.voice.agent console` talks through the Mac's mic.
- **In the browser:** build the web app once with `make web-build` (needs Node.js; `make install` does it too), start the services with `make services-install`, and open http://localhost:8787.

### 4. Google

Gmail, Calendar, Sheets, Docs, Slides and Drive.

1. Paste the OAuth client's JSON (a Desktop app client, from APIs & Services → Credentials) in the control panel, Settings → Logins and keys, which keeps it in the vault. Or put the file in `secrets/`, keeping Google's `client_secret_….json` name (`GOOGLE_CLIENT_SECRET` in `.env` points elsewhere): JARVIS moves it into the vault when it next starts, and you can then delete the file. In that Google Cloud project (APIs & Services → Library), enable:
   - the Gmail API
   - the Google Calendar API
   - the Google Sheets API
   - the Google Docs API
   - the Google Slides API
   - the Google Drive API: used to find files by name or by what's in them, to read Google Slides, and to hold images from the Mac for a moment while a Google Doc copies them. JARVIS only reads Drive files; the only ones it changes are those images, which it made itself.
2. Add each account under a label of your choosing, which signs it in (see [Google accounts](#google-accounts)):
   ```sh
   uv run python -m jarvis.integrations.accounts add work --note "for work"
   uv run python -m jarvis.integrations.accounts add personal --note "my own"
   ```
3. **Google Chat** (Workspace accounts added with `--chat`): see [Google Chat](#google-chat) for the one-time Cloud set-up. Signing such an account in then also asks for Chat.

#### Signing in again

JARVIS asks for these permissions:

| Permission | What it's for |
|---|---|
| Calendar | Your calendar |
| Gmail read | Reading mail |
| Gmail compose | Drafts, and sending email after your yes |
| Gmail modify | Read/unread, archive, star, labels |
| Google Sheets | See, edit and create your spreadsheets |
| Google Docs | See your documents, and see, edit and create them |
| Drive metadata, read-only | File names and details |
| Drive files JARVIS makes itself (`drive.file`) | Images put into docs |
| Drive, read-only (`drive.readonly`) | Searching inside files, and reading Google Slides. Never changes, shares or deletes anything |

An account connected before a permission was added keeps doing everything it could before. When it's asked for something new (changing mail, anything with Google Sheets, reading a Google Doc, formatting or editing one, putting an image from the Mac into one, reading Google Slides, or searching inside Drive files), JARVIS says that account must be signed in again. Drive, read-only is the newest: an account signed in before it still finds files by name, sends and replies to email and changes calendar events (those need no new permission), but reading Google Slides says to sign in again, and so does searching inside files if Google refuses it.

**To sign an account in again:**

1. Run `make google-login LABEL=work` (with that account's label).
2. Sign in with the same Google account in the browser and allow every permission it lists.
3. The new token replaces `secrets/google-<label>.json`. No restart needed.

> [!TIP]
> If the consent screen doesn't show the new permissions, remove JARVIS's access at https://myaccount.google.com/permissions and sign in again.

### 5. iPhone via Tailscale

1. Install Tailscale on the Mac and the iPhone, and sign both in to the same account.
2. Rerun `uv run python -m jarvis.setup_local` so LiveKit advertises the Mac's Tailscale address for audio.
3. Publish the web app and LiveKit signalling over HTTPS, reachable only by your own Tailscale devices:
   ```sh
   tailscale serve --bg 8787
   tailscale serve --bg --set-path /rtc http://127.0.0.1:7880/rtc
   ```
4. On the iPhone, open `https://<mac-name>.<tailnet>.ts.net` in Safari, enter the passcode, then **Share → Add to Home Screen**.
5. Assign an Apple Shortcut that opens that URL to the Action Button.

> [!NOTE]
> If macOS asks whether `livekit-server` may accept incoming connections, allow it; the audio uses UDP port 7882.

### 6. Projects for code tasks

The projects code tasks can work on are kept in the `projects` table of `data/tasks.db`. Add your repos by voice ("add my billing api folder as a project") or from a terminal, with aliases for names speech recognition might mishear:

```sh
uv run python -m jarvis.tasks.projects add billing-api --path ~/code/work/billing-api \
  --aliases "billing api, the billing service" --about "Billing service (work)" --worktrees
uv run python -m jarvis.tasks.projects                                   # list them
uv run python -m jarvis.tasks.projects set billing-api --in-place        # or --path, --aliases, --about
uv run python -m jarvis.tasks.projects remove billing-api                 # off the list; the folder stays
```

Changes apply at once, with no restart.

### 7. Run it 24/7

```sh
uv run python -m jarvis.setup_local --install
```

This installs and starts the four launchd services, labelled `com.jarvis.livekit`, `com.jarvis.agent`, `com.jarvis.web` and `com.jarvis.tasks` (`JARVIS_SERVICE_PREFIX` in `.env` changes the `com.jarvis`). Logs are in `logs/`.

> **Upgrading an install from before these labels** (its services were `com.<name>.jarvis.*`): run `make services-restart`, or `make services-install`, once. Either unloads the services this checkout installed under the old labels, removes their plists from `~/Library/LaunchAgents`, and installs and starts them under the new ones. Only plists that log into this checkout's `logs/` are touched.

| To | Do |
|---|---|
| Keep the Mac awake on the charger | Nothing: `caffeinate` does it |
| Keep it running with the lid closed | `make awake` (`sudo pmset -a disablesleep 1`) |
| Restore normal sleep | `make sleep-normal` |

---

## Settings (`.env`)

`.env.example` lists every setting, with no real values in it. `.env` itself only needs the secrets `setup_local` makes and, for the native app's pushes, your APNs ids: everything else has a default.

Your name, time zone, voice, speed, speech gate, speaker check, task limit and web browser are **changed in the app**, under Settings, and kept in `data/tasks.db` (or on the Mac: `uv run python -m jarvis.state.settings prefs`). Each one's variable below still works, and when it's in `.env` it wins: the app then only shows it.

| In the app | Setting | Applies |
|---|---|---|
| About you | Name, time zone | Without a restart, from the next conversation or task (they're your profile's) |
| Voice | Voice, speed, speech gate; Voice ID: your voiceprint, the speaker check and how sure it must be | From the next conversation |
| System | Background tasks at once | When the task runner restarts |
| System | Web browser on or off, its window shown or hidden | From the next conversation |

### You

| Setting | Effect |
|---|---|
| `JARVIS_USER_NAME` | What JARVIS calls you in its prompts, conversation logs and tasks' instructions. Default: the first name on your Mac account, else "the user". Also in Settings → About you, kept in your profile (`jarvis/core/profile.py`); this wins over it. |
| `JARVIS_TIMEZONE` | Your time zone, as a name like `Europe/London`. Default: the Mac's own, else `UTC`. Also in Settings → About you, kept in your profile; this wins over it. |
| `JARVIS_PASSCODE` | Only for a first setup, which makes one: with no login yet, it lets you in, and the web service then makes it your login (hashed, in `secrets/logins.json`) and takes it out of `.env`. After that, each person's login and passcode are in Settings → Logins and keys; the passcode alone says who's connecting, so no two logins share one. |
| `JARVIS_SERVICE_PREFIX` | The start of the launchd services' labels (default `com.jarvis`, for `com.jarvis.agent` and so on). Change it only to run a second JARVIS under the same Mac account; `make services-restart` then moves the services to the new labels. |

### Voice and listening

| Setting | Effect |
|---|---|
| `JARVIS_CLI_MODEL` | The voice brain's model. Empty uses `claude-opus-5-5`. |
| `JARVIS_EFFORT` | `low` keeps spoken turns quick. |
| `JARVIS_TOOL_SEARCH` | How [connections'](#mcp-servers-and-secrets) tools load, for the voice brain and tasks alike: `true` (default) keeps them out of the conversation until the model looks one up with Claude Code's tool search, so more connections don't slow every reply; `auto:N` defers them only once they'd fill N% of the context window; `false` loads them all up front, as before. JARVIS's own tools always load. |
| `JARVIS_CONNECTION_WAIT` | How many seconds a [connection's](#mcp-servers-and-secrets) call may hold up a spoken reply (default 20). One still running then carries on in the background as a task, and JARVIS says how it went when it ends. `0` waits for every call, as before. Background tasks always wait for theirs. |
| `JARVIS_STT` | The Whisper model, a Hugging Face repo. Empty uses `mlx-community/whisper-large-v3-turbo-q4` (4-bit, about 0.5 GB in memory); `mlx-community/whisper-large-v3-turbo` is the 16-bit original (about 1.6 GB). |
| `JARVIS_STT_PROMPT` | `on` (default) or `off`: give Whisper the speech vocabulary (`data/vocabulary.md`, plus the project names) as its prompt. Off, only known mishearings are put right. Read at the start of each conversation. |
| `JARVIS_TTS_VOICE`, `JARVIS_TTS_SPEED` | Kokoro voice and speed (default `bm_george`, 1.05): `bm_*` British male, `bf_*` British female, `am_*`/`af_*` American. Also in Settings → Preferences. |
| `JARVIS_TTS_MODEL` | The Kokoro model file in `models/`. Empty uses `kokoro-v1.0.onnx`, the full 32-bit one (326 MB); `kokoro-v1.0.int8.onnx` (8-bit, 92 MB) saves about 230 MB but renders 30–60% slower, enough on an 8 GB M2 under load to leave pauses between sentences. When the chosen file is missing JARVIS uses the full one, else the 8-bit one. |
| `JARVIS_TTS_THREADS` | Kokoro's ONNX Runtime threads. Empty: one fewer than the Mac's performance cores, from 2 to 4 (3 on an M1 or M2); `0` is ONNX Runtime's own default, every core. |
| `JARVIS_AUDIO_DIR` | Where [audio files](#audio-files) go (default `~/Downloads`). If macOS keeps JARVIS out of it, they go to `data/audio/` instead, and JARVIS says so. |
| `JARVIS_AUDIO_FORMAT` | The audio files' format when you don't say: `m4a` (default; the Mac's own encoder, about 29 MB an hour), `mp3` (needs `ffmpeg` or `lame`: `brew install ffmpeg`) or `wav` (about 170 MB an hour). |
| `JARVIS_AUDIO_MODEL` | The Kokoro model file audio files are read with. Empty uses `kokoro-v1.0.onnx`, the full 32-bit one, faster than the 8-bit one on Apple silicon; `JARVIS_TTS_MODEL`'s file when it's missing. |
| `JARVIS_AUDIO_THREADS` | CPU threads an audio file is read on (default a quarter of the cores, at least 2), so the live voice keeps the rest. |
| `JARVIS_TURN_MIN_DELAY`, `JARVIS_TURN_MAX_DELAY` | Seconds of silence before your turn is over, to start with: when it sounds finished (default 0.9), and at most when you seem mid-thought (default 4). Each speaker's own delays then follow their pace (kept as `voice_pace:<speaker>` in the settings table; delete the row to start again). |
| `JARVIS_TURN_JUDGE`, `JARVIS_TURN_JUDGE_SECONDS` | When the turn detector is unsure whether a pause ends your turn, a fast model reads what's been said and decides (default on), giving up after 2 s. `off` leaves it to the detector. |
| `JARVIS_VAD_MIN_SILENCE` | Seconds of silence that end an utterance so Whisper can start on it (default 0.55). Every reply waits at least this plus Whisper's time; lower (0.4) answers sooner but cuts more mid-sentence fragments. |
| `JARVIS_VOICE_QUICK_MODEL` | A lighter model (say `claude-sonnet-5`) for turns a fast model judges need no tools and little thought; empty (the default) keeps every turn on the main model. |
| `JARVIS_SIDE_MODEL`, `JARVIS_SIDE_MINUTES` | Background lookups, for slow reading JARVIS hands off so the conversation carries on: their model (default `claude-sonnet-5`) and how long one may run (default 3 minutes). |
| `JARVIS_PROACTIVE_JUDGE` | Whether a fast model picks the moment to bring up task updates, alerts and reminders: now, after "Sorry to cut in" for something urgent, at a better moment, or on the phone (default on). `off` says each as soon as nobody is talking. |
| `JARVIS_WHISPER_UNSURE_LOGPROB` | A turn Whisper heard with an average log-probability under this (default -1.0) is marked as unsure, so JARVIS checks back with its best guess instead of guessing silently. |
| `JARVIS_DENOISE` | The [noise filter](#noise-filter): `on` (default) filters what the VAD, barge-in and Whisper hear; `vad` filters only what the VAD hears, and Whisper gets the original; `off` uses the original everywhere. |
| `JARVIS_DENOISE_ATTEN_DB` | The most the noise filter takes off any sound, in dB (default 18; 0 means no limit). |
| `JARVIS_VOICE_IDLE_PROCESSES` | Spare voice processes kept warm so a call connects at once (default 1). Whisper and Kokoro load once, in a shared process, so a spare takes about 300 MB; 0 saves that, and a call waits a few seconds for its process to start. |
| `JARVIS_MLX_CACHE_MB` | The most GPU memory MLX keeps for reuse after Whisper hears a clip, in MB (default 256; uncapped it held about 0.7 GB). 64 saves 0.2 GB more for about 25 ms a clip; 0 frees it after every clip; -1 is no cap. |
| `JARVIS_TTS_MEMORY_ARENA` | Kokoro's ONNX Runtime memory arena (default off). On, it grows to fit the longest sentence yet and keeps that, about 1 GB after a 20-second sentence; off was as fast. |
| `JARVIS_VOICE_MALLOC_LARGE_CACHE` | Whether macOS's malloc may keep large freed blocks cached in the voice agent's child processes (default off, so freed memory goes back to the system). On, Kokoro's speech process stayed 1 GB bigger even with the arena off. |
| `JARVIS_VOICE_MEMORY_LOG_MINUTES` | How often `logs/agent.log` gets a `voice memory` line, each voice process's footprint and their total (default 10; 0 never). `uv run python -m jarvis.voice.ram` prints the same on demand. |

### Speech gate settings

| Setting | Effect |
|---|---|
| `JARVIS_SPEECH_GATE` | The [speech gate](#speech-gate): `enforce` (default) drops clips it judges phantoms; `shadow` logs them and drops nothing; `off` checks nothing. Also in Settings → Preferences. |
| `JARVIS_SPEECH_GATE_MIN_SPEECH_SECONDS` (0.1), `JARVIS_SPEECH_GATE_MIN_SECONDS` (0.3), `JARVIS_SPEECH_GATE_MIN_DBFS` (-55), `JARVIS_SPEECH_GATE_NO_SPEECH` (0.8), `JARVIS_SPEECH_GATE_WEAK_SPEECH_SECONDS` (0.25), `JARVIS_SPEECH_GATE_FAINT_SPEECH_SECONDS` (0.25), `JARVIS_SPEECH_GATE_FAINT_DBFS` (-28), `JARVIS_SPEECH_GATE_FAINT_MAX_SECONDS` (1.4), `JARVIS_SPEECH_GATE_PHANTOM_DBFS` (-22), `JARVIS_SPEECH_GATE_PHANTOM_SPEECH_SECONDS` (0.6), `JARVIS_SPEECH_GATE_PHANTOMS` (built-in list), `JARVIS_SPEECH_GATE_MIN_LOGPROB` (-1.0), `JARVIS_SPEECH_GATE_UNSURE_SPEECH_SECONDS` (0.5), `JARVIS_SPEECH_GATE_MAX_COMPRESSION` (2.4) | Its thresholds, explained in the [speech gate's table](#speech-gate). |
| `JARVIS_VAD_MIN_SPEECH`, `JARVIS_VAD_THRESHOLD` | The session's Silero VAD: seconds of unbroken speech before an utterance starts (default 0.2; LiveKit's own is 0.05, which a click passes), and the speech probability that counts (default 0.5). Barge-in still needs 0.5 s of speech either way, and with a voiceprint, your voice ([barge-in settings](#speaker-check)). |
| `JARVIS_SPEECH_GATE_SAVE_AUDIO` | On in shadow mode, off otherwise: saves each clip it judges as a WAV in `logs/speech-gate-audio/`. |
| `JARVIS_SPEECH_GATE_AUDIO_KEEP` | How many of the newest saved clips to keep (300). |
| `JARVIS_SPEECH_GATE_SAVE_SHORT` | On by default: with `SAVE_AUDIO` off, still saves the clips it lets through that are under `JARVIS_SPEECH_GATE_SHORT_SECONDS` (2) long or have under `JARVIS_SPEECH_GATE_SHORT_SPEECH_SECONDS` (0.5) of speech. |

### Speaker check settings

| Setting | Effect |
|---|---|
| `JARVIS_SPEAKER_CHECK` | The [speaker check](#speaker-check): `on` (default) drops speech that isn't your voice; `shadow` logs it and drops nothing; `off` checks nothing. Without a voiceprint nothing is checked either way. Also in Settings → Preferences. |
| `JARVIS_SPEAKER_THRESHOLD` | The cosine similarity to your voiceprint an utterance with 3 s of speech or more needs (default 0.35, from 0.1 to 0.8). Also in Settings → Preferences, as Lenient (0.25), Normal or Strict (0.45). |
| `JARVIS_SPEAKER_SHORT_MARGIN` (0.1), `JARVIS_SPEAKER_SHORT_SECONDS` (1), `JARVIS_SPEAKER_LONG_SECONDS` (3), `JARVIS_SPEAKER_RELAX` (0.1), `JARVIS_SPEAKER_MIN_SECONDS` (0.4), `JARVIS_SPEAKER_RECENT_SECONDS` (30), `JARVIS_SPEAKER_REPLY_SECONDS` (10) | How short utterances are judged, explained in [its table](#speaker-check). |
| `JARVIS_SPEAKER_ADAPT` (on), `JARVIS_SPEAKER_ADAPT_MARGIN` (0.1) | Whether your clearly recognised utterances in a call (3 s of speech, the threshold plus the margin) tune its reference to that call's mic and room. |
| `JARVIS_SPEAKER_ENROLL_SECONDS` (25), `JARVIS_SPEAKER_ENROLL_MIN_SECONDS` (12), `JARVIS_SPEAKER_ENROLL_TIMEOUT` (120) | Enrolling by voice: the speech it collects, the least it makes a voiceprint from, and how long it waits. |
| `JARVIS_SPEAKER_BARGE_IN` (on), `JARVIS_SPEAKER_BARGE_IN_MIN_SECONDS` (0.5), `JARVIS_SPEAKER_BARGE_IN_STEP` (0.25), `JARVIS_SPEAKER_BARGE_IN_MAX_SECONDS` (1) | Whether only your voice interrupts JARVIS while it replies (see [Interrupting JARVIS](#speaker-check)); the speech heard before its first check, between checks, and at most in one check. |

### Task settings

| Setting | Effect |
|---|---|
| `JARVIS_MAX_TASKS` | Background tasks running at once (default 6, at most 12); the rest queue. Also in Settings → Preferences. |
| `JARVIS_RESUME_TASKS` | How many background tasks a restart of the task runner cut off, of any kind, carry on by themselves when it starts again (default 3, at most 12; `0` none). Ones cut off mid-command on the Mac or waiting for your go-ahead or an answer always wait for a reply instead. |
| `JARVIS_TASK_MODEL`, `JARVIS_TASK_EFFORT` | Model and effort for background tasks. Empty model uses `claude-opus-5-5`; effort defaults to `high`. |
| `JARVIS_TASK_SEARCH_MODEL`, `JARVIS_TASK_HELPER_MODEL` | Models for a task's subagents: the read-only code searcher (empty uses `claude-haiku-4-5-20251001`), and the test runner and web researcher (empty uses `claude-sonnet-5`). |
| `JARVIS_LOOKUP_MODEL` | The voice brain's read-only lookup subagent. Empty uses `claude-haiku-4-5-20251001`. |
| `JARVIS_NEW_PROJECTS_DIR` | Where a project registered by voice gets its folder when you don't name one. `make setup` asks once and writes it here; unset, the folder JARVIS's own checkout is in (`~/Projects` if that's your home folder). |
| `JARVIS_TASK_ARTIFACTS` | `0` stops tasks publishing mockups and previews to claude.ai as artifacts, whether by default (code-change and research) or granted (on by default; needs JARVIS's Claude login to be a claude.ai subscription, not an API key). They always save the page locally too. `make artifact-check` tests it. |
| `JARVIS_TASK_READ_DIRS` | Folders code tasks may read but not change (comma-separated). `make setup` sets it to your projects folder with `JARVIS_NEW_PROJECTS_DIR`; unset, the same default as that. Secrets in them (`.env` files, keys, credentials, `secrets` folders) stay hidden. |
| `JARVIS_APPROVAL_MINUTES` | How long a risky shell command JARVIS asked about stays open for your yes, and the yes stays good (default 5). |
| `JARVIS_CONSENT`, `JARVIS_CONSENT_MODEL`, `JARVIS_CONSENT_SECONDS` | A spoken reply to an approval question that the yes and no word lists can't read goes to one short model call that sees only your words and what's waiting (`on` by default; `off` keeps to the lists), with this model (default `claude-haiku-4-5-20251001`), given up after this many seconds (default 8), when the reply counts as unclear and you're asked again. Its session starts as soon as JARVIS asks, so it's ready when you answer. |
| `JARVIS_GOAL_LOOP` | `on` (default) or `off`: when a background task says it's finished, one short model call checks the result against what it was asked, and sends it back to finish what it can before you hear about it ([the goal loop](docs/architecture.md#checking-its-finished-the-goal-loop)). |
| `JARVIS_GOAL_MODEL`, `JARVIS_GOAL_ROUNDS` | The check's model (empty uses the tasks' own, `JARVIS_TASK_MODEL`; `claude-sonnet-5` or `claude-haiku-4-5-20251001` are lighter), and how many times a task is sent back per request (default 2, at most 5; 0 checks and reports without sending back). |
| `JARVIS_GOAL_MAX_COST`, `JARVIS_GOAL_DAILY_CHECKS` | Guards: no more sending back once a task has cost this many dollars, as the CLI reports it (default 0, no limit: on a Claude subscription it's a notional figure), and at most this many checks in any 24 hours across all tasks (default 100). |
| `JARVIS_TASK_APPROVAL_HOURS` | How long a background task's request to run a risky command on the Mac waits for your answer before it lapses (default 12). You can answer by voice (straight after JARVIS asks, once per call, or later by telling it "approve it"), with the app's buttons, or with `runner approve N`. It's withdrawn sooner once the task no longer waits on it: it finishes, fails or is stopped, or your reply sends it off on a new run. A task deleting files in its own folder or temp folder, or stopping processes it started, doesn't ask. A yes can also go "for this session" or "always" for that kind of command (installs, stopping processes, permissions, the iPhone); deleting, pushing, sending data off the Mac, system settings and secrets always ask. Settings › Approvals lists and withdraws them. |

### Web browser settings

| Setting | Effect |
|---|---|
| `JARVIS_BROWSER` | `on` (default) or `off`: whether the voice brain gets the [web browser](#web-browser) tools. Also in Settings → Preferences. |
| `JARVIS_BROWSER_WINDOW` | `headed` (default) shows the browser's window, so you can watch it and log in; `headless` hides it. Also in Settings → Preferences. |

### Heartbeat settings

| Setting | Effect |
|---|---|
| `JARVIS_HEARTBEAT` | `on` (default) or `off`: the [heartbeat](#heartbeat). |
| `JARVIS_HEARTBEAT_MINUTES` | Minutes between heartbeat checks (default 15). |
| `JARVIS_TASK_STALL_MINUTES` | A running background task with no sign of progress (no message, tool call, output or log line) for this many minutes is flagged as possibly stuck (default 15; `0` turns it off): marked in `task_list`, `task_detail` and the control panel at once, and said once by voice, Mac notification and phone at the next heartbeat, offering to nudge it or stop it. Waiting for your yes, an answer, Claude's usage limit, or a command or long job still within its time doesn't count. JARVIS never stops a task on its own. |
| `JARVIS_HEARTBEAT_EVENT_MINUTES` | How soon a meeting has to start to be flagged (default 30), unless set in the control panel's Heartbeat page. |
| `JARVIS_HEARTBEAT_QUIET_HOURS` | Local hours with no checks and no alerts (default `23:00-08:00`; empty means never quiet). The [scheduler](#scheduler)'s watches pause in them too. |
| `JARVIS_HEARTBEAT_TRIAGE`, `JARVIS_HEARTBEAT_MODEL` | `on` asks a model to drop newsletters and noise from new mail and GitHub notifications (default `off`). Empty model uses `JARVIS_CLI_MODEL`; `claude-haiku-4-5` is cheaper. |
| `JARVIS_SCHEDULER` | `on` (default) or `off`: the [scheduler](#scheduler). Jobs are kept while it's off, and caught up as usual once it's on. |
| `JARVIS_SCHEDULE_MODEL`, `JARVIS_SCHEDULE_EFFORT` | The model and effort for scheduled tasks and watch checks (default `claude-sonnet-5`, `medium`). Scheduled `code_change` runs use the task model. |
| `JARVIS_VERDICT_MODEL` | The model that reads a finished run's summary for its yes or no, as JSON: whether a watch's check found what it waits for, an "only tell me what's new" run has nothing new, or a fix says its command should run again (default `claude-haiku-4-5-20251001`). |
| `JARVIS_COMMAND_SUMMARY_MODEL` | The model that sums up a command task's (a long job's, or a scheduled command's run's) success in a sentence (default `claude-haiku-4-5-20251001`). |
| `JARVIS_COMMAND_TRIAGE_MODEL` | The model that reads a command task's failure, says why it failed, and decides whether a task fixes it at once, after your yes, or not at all (default `claude-sonnet-5`). |
| `JARVIS_USAGE_HOLD` | How much of Claude's five-hour usage window can be used before scheduled tasks and task-checked watches wait for its reset, leaving the rest for you (default `0.9`; `1` waits only once it's used up). |
| `JARVIS_SIGNIN_WARN_DAYS` | How many days before a GitHub or GitLab token expires the heartbeat warns (default 7). |
| `JARVIS_LEARNING` | `on` (default) or `off`: the [learning review](#learning-review). |
| `JARVIS_LEARNING_MODEL` | The model for the learning review's one call per conversation (default `claude-haiku-4-5-20251001`). |
| `JARVIS_LEARNING_QUIET_MINUTES` | Minutes without a new exchange after which a conversation counts as over and is reviewed (default 10). Hanging up reviews it a minute later. |

### Push settings

| Setting | Effect |
|---|---|
| `JARVIS_PUSH` | `on` sends heartbeat alerts and task updates to phones that tapped Enable notifications (default `on`; `off` sends nothing). See [iPhone notifications](#iphone-notifications). |
| `JARVIS_PUSH_CONTACT` | The `mailto:` or `https:` address push services may use to reach you. Default: your default Google account's address. |
| `JARVIS_APNS_TEAM_ID` | Your Apple developer team id, for pushes to the native JARVIS app. The APNs auth key goes in `secrets/apns_key.p8`, and its key id in Settings → Logins and keys (a `JARVIS_APNS_KEY_ID` here moves there when JARVIS starts). See [iPhone notifications](#iphone-notifications). |

### Brain, CLI and secrets

| Setting | Effect |
|---|---|
| `JARVIS_CLAUDE_CLI` | Path to a `claude` binary. Empty uses the one bundled with `claude-agent-sdk`, which `uv sync` keeps in step with the SDK. |
| `JARVIS_MASTER_KEY` | The key that encrypts the secrets JARVIS keeps in its database, such as the GitHub and GitLab tokens. `make vault ARGS=init` makes it. See [MCP servers and secrets](#mcp-servers-and-secrets). |

> [!NOTE]
> The Google accounts and what JARVIS knows about you are kept in JARVIS's database, `data/tasks.db`, not in `.env`, so the web app can manage them too.

---

## Background tasks

Ask for anything that takes a while and JARVIS starts a background task. You can keep talking, and start more. JARVIS says when each one is done or has a question, and you answer out loud.

| Say | What happens |
|---|---|
| "add retry logic to event backend" · "how does auth work in atlas?" · "find out what changed in LiveKit 1.9" | Starts a background task. |
| "What is task 12 doing?" | Its latest steps: commands, files, subagents and progress. |
| "merge task 12" | Once you say yes, merges the branch into whatever the project's folder has checked out, then deletes the branch and its worktree. |
| "throw away task 12" | Once you say yes, discards it unmerged (an in-place task's changes stay in the folder for you to undo). |
| "start a new project called astro agent" | Makes the folder (in your projects folder, `JARVIS_NEW_PROJECTS_DIR`, unless you name another) and adds it to the list as an in-place project, without git. |
| "set up git for astro agent" | Puts it under git, once the first version has settled. |

**Where code changes go**

- **In place (the default):** changes are made directly in the project's own folder and left uncommitted for you to review, one task at a time per project.
- **In a worktree:** projects set to worktrees (`uv run python -m jarvis.tasks.projects set NAME --worktrees`) get a separate worktree instead, with each task's changes on a `jarvis/<id>-...` branch in the project's repo, committed but never pushed.
- **JARVIS's own repo** always works the worktree way, whatever its entry says.

**What tasks can and can't see**

- Code tasks can also **read, but not change**, your other projects (the folders in `JARVIS_TASK_READ_DIRS`) and JARVIS's own voice session logs, so nothing needs copying in. Secrets stay hidden from them.
- They load the project's own **Claude Code setup**: its CLAUDE.md, subagents, skills and slash commands (as `video-agent:scout-ideas`), but not its settings or hooks, so JARVIS's permissions and sandbox still hold.
- Tasks **can't change JARVIS's own data** (the project list, your to-do list, memory), so they suggest edits instead. JARVIS makes the small ones itself when the task finishes, like adding a to-do or a project, and tells you; it asks before bigger ones, like deleting memory or changing a project's settings.

See [docs/architecture.md](docs/architecture.md#7-background-tasks) for how it works and what tasks can and can't do.

**From a terminal**

```sh
make tasks                                                          # list them
uv run python -m jarvis.tasks.runner show|log|reply|stop|merge|discard|rerun ID   # rerun: a command task's command again
uv run python -m jarvis.tasks.runner edits ID apply|dismiss N...    # a task's suggested edits
```

### To-do list

To-dos live in a `todos` table in `data/tasks.db`, not in memory.

| Say | What happens |
|---|---|
| "Add build an iPhone app to my list" | Puts it on the list. |
| "What's on my list?" | Reads it back as a checklist, open items first. |
| "Tick off the iPhone app" | Marks it done. |
| "Start a task on the iPhone app from my list" | A background task linked to that item: once its work is checked finished, JARVIS ticks the item off and says so, or, if only part is done, offers to reword it to what's left. |

A task you start without naming an item is checked against your open items as it finishes too: one it clearly finishes is offered for ticking off, never ticked unasked. See [the goal loop](docs/architecture.md#checking-its-finished-the-goal-loop).

---

## Heartbeat

JARVIS doesn't only answer when spoken to. Every 15 minutes the task runner checks for these, unless you've switched one off:

| Source | Flags |
|---|---|
| Calendar | Meetings starting in the next 30 minutes |
| Gmail | New unread mail that Gmail marks important (or what your own Gmail search finds) |
| GitHub | Pull requests waiting for your review, and GitHub mentions, assignments and security alerts |
| Google Chat | Unread DMs and @mentions, where Chat is set up. The notification says only who wrote and where ("New Chat message from Alice Carter."), never what, since it shows on the lock screen; the voice adds the first few words. |
| Tasks | Background tasks still waiting for your answer, or cut off by a restart |
| Claude usage | JARVIS getting close to, or out of, its Claude plan's five-hour or weekly limit: once per limit window |
| Sign-ins | A Google account signed out, the `gh` CLI signed out, or the GitHub or GitLab token stopped working or expiring within a week: once, and again only if it's fixed and then breaks again |

**Choosing what it checks.** The control panel's **Settings → Heartbeat** switches each source on or off, and changes its settings: how many minutes ahead a meeting is flagged, and the Gmail search new mail is flagged from. It can also add another Gmail search, with a title of its own ("Invoices": `from:billing@acme.example`), whose new mail is flagged as it arrives. Or say it: "stop flagging GitHub", "flag mail from my bank too". A skill can add a source the same way. Changes apply from the next check; nothing to restart.

**How you hear about it**

- Anything new becomes a **Mac notification**, and a **phone notification** if you've turned those on ([below](#iphone-notifications)).
- If you're in a voice session, JARVIS also **says it at the next pause** ("Heads up. Standup starts in 10 minutes.").

**Rules**

- **Nothing is flagged twice.**
- **Quiet hours:** nothing is checked in quiet hours (11 PM to 8 AM by default); what arrives overnight is flagged at the first check after.
- **Do not disturb:** nothing is checked while it's on either (see [Scheduler](#scheduler)).
- **No backlog:** the first check only records the mail, GitHub and Chat items already there.
- **Read-only:** the heartbeat never sends, creates, starts or runs anything.

`make heartbeat` does a dry run: it prints what would be flagged right now, without notifying anyone or recording anything. Each check writes one line to `logs/tasks.log`. Settings are in the [settings table](#heartbeat-settings); details in [docs/architecture.md](docs/architecture.md#8-heartbeat).

---

## Adding a tool

1. Add a function with `@tool(name, description, properties, required)` in `jarvis/tools/`.
2. If it's in a new module, import that module at the bottom of `jarvis/tools/__init__.py`.

Tools may be sync or async; sync ones run in a thread.

---

## Tests

| Command | What it runs |
|---|---|
| `make test` | The unit tests in `tests/` (pytest is fetched on the fly by `uv run --with pytest`). The web app's tests (`web/tests/`, vitest) run as part of it, and are skipped if its packages aren't installed (`make web-install`). |
| `npm test` (in `web/`) | The web app's tests alone. |
| `make check` | Also typechecks the web app. |
| `make host-check` | The walls around tasks' commands on the Mac, for real: secrets (SSH key files included), JARVIS's files and the runner's socket blocked; PyPI, the task cache, the SSH agent (GitHub over SSH) and code-signing identities reachable. |
| `make artifact-check` | Publishes a tiny claude.ai artifact on JARVIS's login, the way tasks do, and prints the link (a little usage). `FILE=page.html` publishes that page instead. It also runs from a task's worktree, on the main checkout's `.venv` and login (`JARVIS_PYTHON=...` picks another Python). |

