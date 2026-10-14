"""Settings: read once from .env, plus Settings-panel preferences from the settings table (.env wins).

Set-but-empty counts as unset (the default). Task sessions blank every .env variable, and load_dotenv won't fill a
variable that is set, so code they start (e.g. the MCP launcher) sees defaults."""

import os
import pwd
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from jarvis.core import desktop, platform

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
# Vault key; popped from the environment so child processes never inherit it
MASTER_KEY = os.environ.pop("JARVIS_MASTER_KEY", "").strip()


ON, OFF = ("1", "on", "true", "yes"), ("0", "off", "false", "no")


def _switch(value: str, default: bool) -> bool:
    value = value.strip().lower()
    return True if value in ON else False if value in OFF else default


def _flag(name: str, default: bool) -> bool:
    """On/off variable: 1/on/true/yes or 0/off/false/no; anything else is `default`."""
    return _switch(os.environ.get(name, ""), default)


# Install data folder, also the owner's profile folder. Tests override JARVIS_DATA_DIR.
DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "") or ROOT / "data")
SECRETS_DIR = ROOT / "secrets"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SECRETS_DIR.mkdir(exist_ok=True)
# Install database: profiles, plus the owner's tasks, to-dos, projects, accounts, settings and secrets
DB_FILE = Path(os.environ.get("JARVIS_DB_FILE", "") or DATA_DIR / "tasks.db")

# Settings-table key -> .env variable that overrides it. Name and time zone live on the profile (env wins for the
# owner). max_tasks is read at service start; voice, speed and speech gate per conversation.
PREFERENCES = {"name": "JARVIS_USER_NAME", "timezone": "JARVIS_TIMEZONE", "voice": "JARVIS_TTS_VOICE",
               "voice_speed": "JARVIS_TTS_SPEED", "speech_gate": "JARVIS_SPEECH_GATE", "max_tasks": "JARVIS_MAX_TASKS",
               "browser": "JARVIS_BROWSER", "browser_window": "JARVIS_BROWSER_WINDOW",
               "speaker_check": "JARVIS_SPEAKER_CHECK", "speaker_threshold": "JARVIS_SPEAKER_THRESHOLD",
               "injection_check": "JARVIS_INJECTION_CHECK", "injection_model": "JARVIS_INJECTION_MODEL"}
# Fallbacks when neither is set; name and time zone fall back to mac_name() / mac_timezone()
DEFAULTS = {"voice": "bm_george", "voice_speed": "1.05", "speech_gate": "enforce", "max_tasks": "6",
            "browser": "on", "browser_window": "headed", "speaker_check": "on", "speaker_threshold": "0.35",
            "injection_check": "on", "injection_model": "claude-haiku-4-5-20251001"}
SPEED_RANGE, MAX_TASKS_RANGE, SPEAKER_THRESHOLD_RANGE = (0.5, 2.0), (1, 12), (0.1, 0.8)


def stored(key: str, db_file: Path | None = None) -> str:
    """Preference from the settings table of `db_file` (default: current profile's DB); "" if absent or unreadable."""
    if db_file is None:
        from jarvis.core import profile  # circular import
        db_file = profile.current().db
    try:
        with closing(sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)) as db:
            row = db.execute("select value from settings where key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return ""
    return row[0].strip() if row else ""


def preference(key: str) -> str:
    """Preference from .env, else the settings table; "" means use the caller's default."""
    return os.environ.get(PREFERENCES[key], "").strip() or stored(key)


def _number(value: str, default: float, low: float, high: float) -> float:
    try:
        return min(high, max(low, float(value)))
    except ValueError:
        return default


def mac_name() -> str:
    """First name of the computer's user account ("Sam" from "Sam Lee"); "" if none."""
    try:
        full = pwd.getpwuid(os.getuid()).pw_gecos
    except KeyError:
        return ""
    return full.split(",")[0].split()[0] if full.strip() else ""


def mac_timezone() -> str:
    """This machine's time zone from the /etc/localtime symlink target, else (Linux) /etc/timezone, timedatectl or
    TZ; "" if unknown."""
    _, found, zone = os.path.realpath("/etc/localtime").partition("/zoneinfo/")
    if found and zone_ok(zone):
        return zone
    if not platform.LINUX:
        return ""
    for zone in _linux_zones():
        zone = zone.strip().lstrip(":")
        if zone and zone_ok(zone):
            return zone
    return ""


def _linux_zones(etc_timezone: Path = Path("/etc/timezone")):
    """Time zone candidates where Linux keeps them when /etc/localtime is a copy, not a symlink."""
    try:
        yield etc_timezone.read_text()
    except OSError:
        pass
    try:
        yield subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True, text=True,
                             timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        pass
    yield os.environ.get("TZ", "")


def zone_ok(zone: str) -> bool:
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


# Name the assistant answers to (used by the safety scan)
ASSISTANT_NAME = "JARVIS"

# Empty = CLI bundled with claude-agent-sdk, so its version tracks the SDK
CLAUDE_CLI = os.environ.get("JARVIS_CLAUDE_CLI", "")
# Claude login: the user's own Claude Code sign-in, unless JARVIS_CLAUDE_CONFIG_DIR gives JARVIS one of its own
# (relative to ROOT, e.g. secrets/claude). Only that one is passed to the CLI: setting CLAUDE_CONFIG_DIR at all, even to
# ~/.claude, makes the CLI look for a different macOS Keychain entry, so the user's own login wouldn't be found.
CLAUDE_OWN_CONFIG_DIR = (os.environ.get("JARVIS_CLAUDE_CONFIG_DIR", "")
                         and str(ROOT / Path(os.environ["JARVIS_CLAUDE_CONFIG_DIR"]).expanduser()))
# The CLI's folder either way: its session transcripts, and on Linux the login
CLAUDE_CONFIG_DIR = (CLAUDE_OWN_CONFIG_DIR or os.environ.get("CLAUDE_CONFIG_DIR", "")
                     or str(Path.home() / ".claude"))
DEFAULT_MODEL = "claude-opus-5-5"
CLI_MODEL = os.environ.get("JARVIS_CLI_MODEL", "") or DEFAULT_MODEL
FAST_MODEL = "claude-haiku-4-5-20251001"  # subagents that only look things up
HELPER_MODEL = "claude-sonnet-5"  # subagents that need more judgment: test runs, web research
# Read-only file lookups (file_lookup, jarvis.brain.side); keeps file contents out of the spoken turn
LOOKUP_MODEL = os.environ.get("JARVIS_LOOKUP_MODEL", "") or FAST_MODEL
# Seconds one spoken turn may wait on file lookups in all; past that they carry on in the background, announced
LOOKUP_WAIT = _number(os.environ.get("JARVIS_LOOKUP_WAIT", ""), 8.0, 0.0, 60.0)
EFFORT = os.environ.get("JARVIS_EFFORT", "") or "low"  # low keeps spoken replies fast
# Lighter model for voice turns judged to need no tools (jarvis.brain.route); empty = always CLI_MODEL
VOICE_QUICK_MODEL = os.environ.get("JARVIS_VOICE_QUICK_MODEL", "")
# Background lookups handed off by the voice brain (jarvis.brain.side): model and time limit (file lookups too)
SIDE_MODEL = os.environ.get("JARVIS_SIDE_MODEL", "") or HELPER_MODEL
SIDE_MINUTES = _number(os.environ.get("JARVIS_SIDE_MINUTES", ""), 3.0, 0.5, 30.0)
# A fast model picks when to speak updates and reminders (jarvis.voice.floor); off = as soon as nobody is talking
PROACTIVE_JUDGE = _flag("JARVIS_PROACTIVE_JUDGE", True)
# CLI ENABLE_TOOL_SEARCH for MCP tools (JARVIS's own always load): true = always deferred, auto:N = deferred once they'd
# fill N% of context, false = all loaded up front. Deferring keeps many connections from slowing every reply.
TOOL_SEARCH = (os.environ.get("JARVIS_TOOL_SEARCH", "") or "true").strip().lower()
# Seconds an MCP call may block a spoken reply before continuing in the background (jarvis.integrations.mcp_relay).
# 0 = always wait. Tasks always wait.
CONNECTION_WAIT = _number(os.environ.get("JARVIS_CONNECTION_WAIT", ""), 20.0, 0.0, 600.0)
# Seconds one of JARVIS's own tools (a merge, a long read) may block a spoken reply before carrying on in the
# background, its result coming back in a brain turn of its own (jarvis.brain.kept). 0 = always wait. Tasks always wait.
TOOL_WAIT = _number(os.environ.get("JARVIS_TOOL_WAIT", ""), 8.0, 0.0, 600.0)
MAX_TOOL_ROUNDS = int(os.environ.get("JARVIS_MAX_TOOL_ROUNDS", "") or "8")
# How long a risky command awaits a spoken yes, and how long that yes stays valid
APPROVAL_MINUTES = max(1, int(os.environ.get("JARVIS_APPROVAL_MINUTES", "") or "5"))
# Replies the yes/no word lists can't read go to CONSENT_MODEL (jarvis.safety.consent); unclear after CONSENT_SECONDS.
# Keep CONSENT_SECONDS well above CLI startup time. JARVIS_CONSENT=off = word lists only.
CONSENT_ENABLED = _flag("JARVIS_CONSENT", True)
CONSENT_MODEL = os.environ.get("JARVIS_CONSENT_MODEL", "") or FAST_MODEL
CONSENT_SECONDS = _number(os.environ.get("JARVIS_CONSENT_SECONDS", ""), 8.0, 0.5, 15.0)
# Prompt-injection screen on tool results (jarvis.safety.injection). On/off and model are preferences. A check that
# times out passes the result through, marked unchecked.
INJECTION_VOICE_SECONDS = _number(os.environ.get("JARVIS_INJECTION_VOICE_SECONDS", ""), 6.0, 1.0, 30.0)
INJECTION_TASK_SECONDS = _number(os.environ.get("JARVIS_INJECTION_TASK_SECONDS", ""), 30.0, 1.0, 120.0)


def projects_home() -> Path:
    """Default projects folder: the parent of this checkout, or ~/Projects if that parent is $HOME or a disk root.

    Too broad a parent would let tasks read far too much. A task worktree (data/worktrees/<name>) resolves to its
    main checkout."""
    root = ROOT.parents[2] if ROOT.parent.name == "worktrees" and ROOT.parent.parent.name == "data" else ROOT
    home = root.parent
    return Path("~/Projects").expanduser() if home in (Path.home(), Path(home.anchor)) else home


PROJECTS_HOME = projects_home()

# Task runner (jarvis.tasks.runner), separate from voice sessions
RUNNER_SOCKET = DATA_DIR / "runner.sock"
# project_register default location: NEW_PROJECTS_DIR/<name>
NEW_PROJECTS_DIR = Path(os.environ.get("JARVIS_NEW_PROJECTS_DIR", "") or PROJECTS_HOME).expanduser()
WORKTREES_DIR = DATA_DIR / "worktrees"
TASK_CACHE_DIR = DATA_DIR / "task-cache"  # build and package caches for sandboxed commands
MAX_TASKS = int(_number(preference("max_tasks"), float(DEFAULTS["max_tasks"]), *MAX_TASKS_RANGE))  # running at once; the rest queue
TASK_MODEL = os.environ.get("JARVIS_TASK_MODEL", "") or DEFAULT_MODEL
TASK_EFFORT = os.environ.get("JARVIS_TASK_EFFORT", "") or "high"
TASK_MAX_TURNS = int(os.environ.get("JARVIS_TASK_MAX_TURNS", "") or "150")
# Task subagents: read-only code search; test runner / web research
TASK_SEARCH_MODEL = os.environ.get("JARVIS_TASK_SEARCH_MODEL", "") or FAST_MODEL
TASK_HELPER_MODEL = os.environ.get("JARVIS_TASK_HELPER_MODEL", "") or HELPER_MODEL
# How long a task's risky-command request waits for the user
TASK_APPROVAL_HOURS = max(1.0, float(os.environ.get("JARVIS_TASK_APPROVAL_HOURS", "") or "12"))
# Minutes without progress before a task is flagged stalled (jarvis.tasks.stall); 0 = never
TASK_STALL_MINUTES = int(_number(os.environ.get("JARVIS_TASK_STALL_MINUTES", ""), 15, 0, 24 * 60))
# Tasks interrupted by a restart to resume on runner start; 0 = none
RESUME_TASKS = int(_number(os.environ.get("JARVIS_RESUME_TASKS", ""), 3, 0, 12))
# Allow the Artifact tool (claude.ai pages; needs a subscription login, not an API key) for code_change and research
# tasks, others when granted (jarvis.tasks.access). 0 = off for all.
TASK_ARTIFACTS = _flag("JARVIS_TASK_ARTIFACTS", True)
# Read-only folders for code tasks (secrets inside stay hidden). Set but empty = none.
TASK_READ_DIRS = [
    Path(d.strip()).expanduser()
    for d in os.environ.get("JARVIS_TASK_READ_DIRS", str(PROJECTS_HOME)).split(",") if d.strip()
]
# Goal loop (jarvis.tasks.goal): a "finished" task is checked against its request and sent back up to GOAL_ROUNDS times.
# GOAL_MAX_COST: CLI-reported dollars after which it stops (0 = no limit). GOAL_DAILY_CHECKS: cap per 24 h, all tasks.
GOAL_LOOP = _flag("JARVIS_GOAL_LOOP", True)
GOAL_MODEL = os.environ.get("JARVIS_GOAL_MODEL", "").strip() or TASK_MODEL
GOAL_ROUNDS = int(_number(os.environ.get("JARVIS_GOAL_ROUNDS", ""), 2, 0, 5))
GOAL_MAX_COST = _number(os.environ.get("JARVIS_GOAL_MAX_COST", ""), 0.0, 0.0, 10000.0)
GOAL_DAILY_CHECKS = int(_number(os.environ.get("JARVIS_GOAL_DAILY_CHECKS", ""), 100, 1, 10000))
# Task-end announcement (jarvis.tasks.announce), worded by ANNOUNCE_MODEL; fixed wording on timeout or when off
ANNOUNCE_ENABLED = _flag("JARVIS_ANNOUNCE", True)
ANNOUNCE_MODEL = os.environ.get("JARVIS_ANNOUNCE_MODEL", "") or FAST_MODEL
ANNOUNCE_SECONDS = _number(os.environ.get("JARVIS_ANNOUNCE_SECONDS", ""), 30.0, 5.0, 120.0)

# Other machines (jarvis.node). Where jobs run is a per-user setting (Settings › Machines); when it lets JARVIS choose,
# PLACEMENT_MODEL picks among the machines online. A machine unreachable for NODE_LOST_MINUTES while it has work is
# reported once; a task whose session lives on a machine waits NODE_WAIT_MINUTES for it to come back. Copies of a
# project sent to a machine stop at NODE_SYNC_MAX_GB.
PLACEMENT_MODEL = os.environ.get("JARVIS_PLACEMENT_MODEL", "") or FAST_MODEL
NODE_LOST_MINUTES = _number(os.environ.get("JARVIS_NODE_LOST_MINUTES", ""), 30, 1, 24 * 60)
NODE_WAIT_MINUTES = _number(os.environ.get("JARVIS_NODE_WAIT_MINUTES", ""), 30, 0, 24 * 60)
NODE_SYNC_MAX_GB = _number(os.environ.get("JARVIS_NODE_SYNC_MAX_GB", ""), 20, 0.1, 10000)

# Notices (jarvis.tasks.notices): JARVIS's own news, said unasked: tasks stuck or waiting, usage limits, machines lost
NOTICES_ENABLED = _flag("JARVIS_NOTICES", True)
# Local hours with no watch checks or notices (jarvis.tasks.hold); empty = never quiet
QUIET_HOURS = os.environ.get("JARVIS_QUIET_HOURS", "23:00-08:00")

# Scheduler (jarvis.tasks.scheduler): reminders, repeating tasks and watches
SCHEDULER_ENABLED = _flag("JARVIS_SCHEDULER", True)
# Lighter model for routine scheduled runs and watches (code_change keeps TASK_MODEL)
SCHEDULE_MODEL = os.environ.get("JARVIS_SCHEDULE_MODEL", "") or HELPER_MODEL
SCHEDULE_EFFORT = os.environ.get("JARVIS_SCHEDULE_EFFORT", "") or "medium"
# Reads a yes/no verdict from a run's summary: watch hit, only_changes found nothing new, fix wants a rerun
VERDICT_MODEL = os.environ.get("JARVIS_VERDICT_MODEL", "") or FAST_MODEL
# Command jobs (jarvis.tasks.command_jobs): success summarised from output tail; failure triaged (why, and whether to
# auto-fix, ask first, or leave it)
COMMAND_SUMMARY_MODEL = os.environ.get("JARVIS_COMMAND_SUMMARY_MODEL", "") or FAST_MODEL
COMMAND_TRIAGE_MODEL = os.environ.get("JARVIS_COMMAND_TRIAGE_MODEL", "") or HELPER_MODEL
# Plan usage last reported by the CLI (jarvis.brain.usage). Scheduled model runs wait for the 5-hour reset once
# USAGE_HOLD of it is used (1 = never), and for any window that is exhausted.
USAGE_FILE = DATA_DIR / "usage.json"
USAGE_HOLD = _number(os.environ.get("JARVIS_USAGE_HOLD", ""), 0.9, 0.5, 1.0)

# Learning review (jarvis.tasks.learning): after a conversation ends (hang-up or quiet), suggest memory edits
LEARNING_ENABLED = _flag("JARVIS_LEARNING", True)
LEARNING_MODEL = os.environ.get("JARVIS_LEARNING_MODEL", "") or FAST_MODEL
LEARNING_QUIET_MINUTES = max(1, int(os.environ.get("JARVIS_LEARNING_QUIET_MINUTES", "") or "10"))

# Skills (jarvis.skills): Claude Code SKILL.md folders. Scripts in installed skills are reviewed before enabling.
SKILL_REVIEW_MODEL = os.environ.get("JARVIS_SKILL_REVIEW_MODEL", "") or HELPER_MODEL

# Web Push (jarvis.notify.push): alerts and task updates to the home-screen web app
PUSH_ENABLED = _flag("JARVIS_PUSH", True)  # sends nothing until a device subscribes
# VAPID "sub" claim (mailto: or https:); unset = default Google account's address
PUSH_CONTACT = os.environ.get("JARVIS_PUSH_CONTACT", "").strip()
VAPID_KEY_FILE = SECRETS_DIR / "vapid_private.pem"  # generated on first use
# APNs (jarvis.notify.apns) for the iOS app, whose web view lacks Web Push. Key from developer.apple.com > Keys;
# its key id is in the vault.
APNS_KEY_FILE = SECRETS_DIR / "apns_key.p8"
APNS_TEAM_ID = os.environ.get("JARVIS_APNS_TEAM_ID", "").strip()  # Membership details
# FCM (jarvis.notify.fcm) for the Android app: a Firebase service-account key (Project settings > Service accounts)
FCM_SERVICE_ACCOUNT_FILE = SECRETS_DIR / "fcm_service_account.json"

# On-device voice: Whisper in, Kokoro out. Whisper runs on MLX on Apple silicon, else on faster-whisper
# (jarvis.core.whisper). JARVIS_STT names the model: on MLX 4-bit uses ~0.5 GB vs 1.6 GB at 16-bit (matters on 8 GB);
# empty elsewhere = large-v3-turbo on an NVIDIA GPU, small.en on the CPU.
STT_MODEL = os.environ.get("JARVIS_STT", "") or (
    "mlx-community/whisper-large-v3-turbo-q4" if platform.APPLE_SILICON else "")
# faster-whisper's device: cuda or cpu; empty = cuda when an NVIDIA GPU is usable
STT_DEVICE = os.environ.get("JARVIS_STT_DEVICE", "").strip().lower()
# Recordings (jarvis.core.transcribe) can be in any language: empty off Apple silicon = a multilingual default
TRANSCRIBE_MODEL = os.environ.get("JARVIS_TRANSCRIBE_MODEL", "") or (STT_MODEL if platform.APPLE_SILICON else "")
# Prompt Whisper with the speech vocabulary (jarvis.state.vocabulary); off = only fix known mishearings
STT_PROMPT = _flag("JARVIS_STT_PROMPT", True)
# Warm spare job processes so calls connect instantly (~300 MB each; models live in the shared speech process).
# 0 saves the memory; a call then waits a few seconds.
VOICE_IDLE_PROCESSES = max(0, int(os.environ.get("JARVIS_VOICE_IDLE_PROCESSES", "") or "1"))
# MLX buffer-cache cap in MB (freed Whisper buffers, ~0.7 GB per clip uncapped). 256 costs nothing measurable;
# 64 saves 0.2 GB for ~25 ms/clip; 0 = free after every clip; -1 = no cap. Apple silicon only.
MLX_CACHE_MB = int(os.environ.get("JARVIS_MLX_CACHE_MB", "") or "256")
# Kokoro ONNX memory arena: never shrinks (~1 GB after a 20 s sentence) and gave no speedup, so off by default
TTS_MEMORY_ARENA = _flag("JARVIS_TTS_MEMORY_ARENA", False)
# Allocator caching of large freed blocks in voice child processes (macOS's large cache, glibc's growing mmap
# threshold). Off so freed memory returns to the system.
VOICE_MALLOC_LARGE_CACHE = _flag("JARVIS_VOICE_MALLOC_LARGE_CACHE", False)
# Minutes between voice-process memory logs (jarvis.voice.ram); 0 = never
VOICE_MEMORY_LOG_MINUTES = max(0.0, float(os.environ.get("JARVIS_VOICE_MEMORY_LOG_MINUTES", "") or "10"))
# Silence in seconds after the last speech that ends a turn. Counted from when speech stopped, so the VAD's silence
# (VAD_MIN_SILENCE, its floor) and Whisper's time fall inside it; the turn also waits for Whisper's words
# (jarvis.voice.turn.Transcribed). Lower answers sooner but cuts off more mid-thought pauses (a turn that goes on
# before the reply is heard is answered together with the next).
TURN_SILENCE = _number(os.environ.get("JARVIS_TURN_SILENCE", ""), 0.8, 0.3, 5.0)
# Below this average log-prob a turn is marked unsure, so the brain may confirm its guess
WHISPER_UNSURE_LOGPROB = _number(os.environ.get("JARVIS_WHISPER_UNSURE_LOGPROB", ""), -1.0, -10.0, 0.0)
# Silero VAD: speech needed to start an utterance and the probability threshold. LiveKit's 0.05 s default lets a
# click through and Whisper hears a phantom "Thank you.". Barge-in still needs 0.5 s.
VAD_MIN_SPEECH = float(os.environ.get("JARVIS_VAD_MIN_SPEECH", "") or "0.2")
# Silence that ends an utterance (LiveKit default). Floor on every reply's latency; lower = more mid-sentence cuts.
VAD_MIN_SILENCE = _number(os.environ.get("JARVIS_VAD_MIN_SILENCE", ""), 0.55, 0.3, 2.0)
VAD_THRESHOLD = float(os.environ.get("JARVIS_VAD_THRESHOLD", "") or "0.5")
VOICE_TURNS_LOG = ROOT / "logs" / "voice-turns.jsonl"  # per-stage turn timing


def tts_voice() -> str:
    """Kokoro voice id: b*/a* = British/American, then m/f."""
    return preference("voice") or DEFAULTS["voice"]


def tts_speed() -> float:
    return _number(preference("voice_speed"), float(DEFAULTS["voice_speed"]), *SPEED_RANGE)


TTS_VOICE, TTS_SPEED = tts_voice(), tts_speed()
# Kokoro model (name in models/ or full path); first existing fallback if missing. 32-bit by default: int8 saves
# ~230 MB but renders 30-60% slower on an M2, leaving pauses between sentences under load.
TTS_MODEL = ROOT / "models" / (os.environ.get("JARVIS_TTS_MODEL", "") or "kokoro-v1.0.onnx")
TTS_MODEL_FALLBACKS = (ROOT / "models/kokoro-v1.0.onnx", ROOT / "models/kokoro-v1.0.int8.onnx")
# Kokoro threads. Empty = performance cores - 1, clamped to 2..4 (as fast as 8 under load, frees a core for the
# call); 0 = ONNX Runtime default.
TTS_THREADS = os.environ.get("JARVIS_TTS_THREADS", "").strip()
# Audio files (jarvis.voice.audio_file): text-to-file in a separate low-priority Kokoro process, leaving cores for the
# live voice. AUDIO_MODEL falls back to TTS_MODEL. AUDIO_THREADS 0 = a quarter of the cores, at least 2.
AUDIO_DIR = Path(os.environ.get("JARVIS_AUDIO_DIR", "") or desktop.user_dir("DOWNLOAD", "Downloads")).expanduser()
AUDIO_FORMATS = ("m4a", "mp3", "wav")
AUDIO_FORMAT = (os.environ.get("JARVIS_AUDIO_FORMAT", "").strip().lower() or "m4a").removeprefix(".")
if AUDIO_FORMAT not in AUDIO_FORMATS:
    AUDIO_FORMAT = "m4a"
AUDIO_MODEL = ROOT / "models" / (os.environ.get("JARVIS_AUDIO_MODEL", "") or "kokoro-v1.0.onnx")
AUDIO_THREADS = max(0, int(os.environ.get("JARVIS_AUDIO_THREADS", "") or "0"))  # 0 = auto
# Mic noise suppression (jarvis.voice.denoise, DeepFilterNet 3 on CPU). on = filtered everywhere; vad = VAD only,
# Whisper hears the original; off = never
DENOISE_MODES = ("on", "vad", "off")
_denoise = os.environ.get("JARVIS_DENOISE", "").strip().lower()
DENOISE = {"1": "on", "true": "on", "yes": "on", "0": "off", "false": "off", "no": "off"}.get(_denoise, _denoise)
if DENOISE not in DENOISE_MODES:
    DENOISE = "on"
# Max attenuation in dB, so speech mistaken for noise stays audible; 0 = no limit
DENOISE_ATTEN_DB = max(0.0, float(os.environ.get("JARVIS_DENOISE_ATTEN_DB", "") or "18"))
DENOISE_MODEL = ROOT / "models/deepfilternet3-streaming.onnx"
# Speech gate (jarvis.voice.speech_gate): drops clips without real speech, judged on audio; known Whisper phantoms
# face a higher bar. shadow = log only; enforce = drop; off = no checks.
SPEECH_GATE_MODES = ("shadow", "enforce", "off")


def speech_gate() -> str:
    mode = preference("speech_gate").lower()
    return mode if mode in SPEECH_GATE_MODES else DEFAULTS["speech_gate"]


SPEECH_GATE = speech_gate()
SPEECH_GATE_MIN_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_MIN_SPEECH_SECONDS", "") or "0.1")
SPEECH_GATE_MIN_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_MIN_SECONDS", "") or "0.3")
SPEECH_GATE_MIN_DBFS = float(os.environ.get("JARVIS_SPEECH_GATE_MIN_DBFS", "") or "-55")
SPEECH_GATE_NO_SPEECH = float(os.environ.get("JARVIS_SPEECH_GATE_NO_SPEECH", "") or "0.8")
SPEECH_GATE_WEAK_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_WEAK_SPEECH_SECONDS", "") or "0.25")
# Faint: little speech AND quiet AND short (phantoms that pass the checks above)
SPEECH_GATE_FAINT_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_FAINT_SPEECH_SECONDS", "") or "0.25")
SPEECH_GATE_FAINT_DBFS = float(os.environ.get("JARVIS_SPEECH_GATE_FAINT_DBFS", "") or "-28")
SPEECH_GATE_FAINT_MAX_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_FAINT_MAX_SECONDS", "") or "1.4")
# A transcript that is only a known phantom ("Thank you.", "Bye.") counts only if loud enough, long enough and
# confident. "yes"/"no"/"okay" are never on the list. JARVIS_SPEECH_GATE_PHANTOMS (comma-separated) replaces it.
SPEECH_GATE_PHANTOM_DBFS = float(os.environ.get("JARVIS_SPEECH_GATE_PHANTOM_DBFS", "") or "-22")
SPEECH_GATE_PHANTOM_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_PHANTOM_SPEECH_SECONDS", "") or "0.6")
SPEECH_GATE_PHANTOMS = [p.strip() for p in os.environ.get("JARVIS_SPEECH_GATE_PHANTOMS", "").split(",") if p.strip()]
# Unsure: avg log-prob below Whisper's retry threshold AND little speech
SPEECH_GATE_MIN_LOGPROB = float(os.environ.get("JARVIS_SPEECH_GATE_MIN_LOGPROB", "") or "-1.0")
SPEECH_GATE_UNSURE_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_UNSURE_SPEECH_SECONDS", "") or "0.5")
# Repetitive: gzip ratio over Whisper's threshold, i.e. a looping hallucination
SPEECH_GATE_MAX_COMPRESSION = float(os.environ.get("JARVIS_SPEECH_GATE_MAX_COMPRESSION", "") or "2.4")
SPEECH_GATE_LOG = ROOT / "logs" / "speech-gate.jsonl"  # every clip measured, kept or not
# Save each judged clip as WAV; on by default in shadow mode


def speech_gate_save_audio(mode: str) -> bool:
    return _flag("JARVIS_SPEECH_GATE_SAVE_AUDIO", mode == "shadow")


SPEECH_GATE_SAVE_AUDIO = speech_gate_save_audio(SPEECH_GATE)
# Save short clips that pass even with SAVE_AUDIO off, to catch phantoms that slip through
SPEECH_GATE_SAVE_SHORT = _flag("JARVIS_SPEECH_GATE_SAVE_SHORT", True)
SPEECH_GATE_SHORT_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_SHORT_SECONDS", "") or "2.0")
SPEECH_GATE_SHORT_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_SHORT_SPEECH_SECONDS", "") or "0.5")
SPEECH_GATE_AUDIO_DIR = ROOT / "logs" / "speech-gate-audio"
SPEECH_GATE_AUDIO_KEEP = max(1, int(os.environ.get("JARVIS_SPEECH_GATE_AUDIO_KEEP", "") or "300"))  # newest files kept

# Speaker verification (jarvis.voice.speaker) against the enrolled voiceprint; drops other voices (TV, others).
# on = drop; shadow = log only; off = no checks. No effect until a voiceprint exists.
SPEAKER_MODES = ("on", "shadow", "off")
SPEAKER_MODEL = ROOT / "models/wespeaker-voxceleb-resnet34-LM.onnx"  # `make models`; voiceprints record their model
SPEAKER_MODEL_ID = "wespeaker-voxceleb-resnet34-LM"


def speaker_check() -> str:
    mode = preference("speaker_check").lower()
    return mode if mode in SPEAKER_MODES else DEFAULTS["speaker_check"]


def speaker_threshold() -> float:
    """Cosine similarity needed for an utterance of at least SPEAKER_LONG_SECONDS of speech."""
    return _number(preference("speaker_threshold"), float(DEFAULTS["speaker_threshold"]), *SPEAKER_THRESHOLD_RANGE)


def _env_number(name: str, default: float, low: float = 0.0, high: float = 1e6) -> float:
    return _number(os.environ.get(name, "").strip() or str(default), default, low, high)


# Short utterances are less telling: threshold - SHORT_MARGIN at <= SHORT_SECONDS, rising to full at LONG_SECONDS.
# Mid-conversation (user heard within RECENT_SECONDS, or a reply ended within REPLY_SECONDS) subtract RELAX too,
# and keep anything under MIN_SECONDS regardless, so a quick "yes" isn't lost.
SPEAKER_SHORT_MARGIN = _env_number("JARVIS_SPEAKER_SHORT_MARGIN", 0.1, 0.0, 1.0)
SPEAKER_RELAX = _env_number("JARVIS_SPEAKER_RELAX", 0.1, 0.0, 1.0)
SPEAKER_SHORT_SECONDS = _env_number("JARVIS_SPEAKER_SHORT_SECONDS", 1.0)
SPEAKER_LONG_SECONDS = _env_number("JARVIS_SPEAKER_LONG_SECONDS", 3.0)
SPEAKER_MIN_SECONDS = _env_number("JARVIS_SPEAKER_MIN_SECONDS", 0.4)
SPEAKER_RECENT_SECONDS = _env_number("JARVIS_SPEAKER_RECENT_SECONDS", 30.0)
SPEAKER_REPLY_SECONDS = _env_number("JARVIS_SPEAKER_REPLY_SECONDS", 10.0)
# Use clearly verified utterances (long, ADAPT_MARGIN over threshold) as extra references for this call's mic/room
SPEAKER_ADAPT = _flag("JARVIS_SPEAKER_ADAPT", True)
SPEAKER_ADAPT_MARGIN = _env_number("JARVIS_SPEAKER_ADAPT_MARGIN", 0.1, 0.0, 1.0)
# Enrollment: target speech (silence excluded), minimum accepted, and spoken-enrollment timeout
SPEAKER_ENROLL_SECONDS = _env_number("JARVIS_SPEAKER_ENROLL_SECONDS", 25.0, 5.0, 300.0)
SPEAKER_ENROLL_MIN_SECONDS = _env_number("JARVIS_SPEAKER_ENROLL_MIN_SECONDS", 12.0, 3.0, 300.0)
SPEAKER_ENROLL_TIMEOUT = _env_number("JARVIS_SPEAKER_ENROLL_TIMEOUT", 120.0, 10.0, 3600.0)
SPEAKER_LOG = ROOT / "logs" / "speaker-check.jsonl"  # score, threshold, verdict per utterance
# Barge-in (jarvis.voice.barge_in) only for the verified user: first judged at MIN_SECONDS of speech, then every STEP
# on the latest MAX_SECONDS until it matches or stops. off = any voice interrupts.
SPEAKER_BARGE_IN = _flag("JARVIS_SPEAKER_BARGE_IN", True)
SPEAKER_BARGE_IN_MIN_SECONDS = _env_number("JARVIS_SPEAKER_BARGE_IN_MIN_SECONDS", 0.5, 0.2, 5.0)
SPEAKER_BARGE_IN_MAX_SECONDS = max(SPEAKER_BARGE_IN_MIN_SECONDS,
                                   _env_number("JARVIS_SPEAKER_BARGE_IN_MAX_SECONDS", 1.0, 0.2, 6.0))
SPEAKER_BARGE_IN_STEP = _env_number("JARVIS_SPEAKER_BARGE_IN_STEP", 0.25, 0.05, 2.0)

# Browser (jarvis.integrations.browser): Playwright-driven Chrome with its own profile per user. Preferences: browser
# tools on/off (next conversation) and headed/headless (next launch).
BROWSER_WINDOWS = ("headed", "headless")


def browser_on() -> bool:
    return _switch(preference("browser"), True)


def browser_headless() -> bool:
    return preference("browser_window").lower() == "headless"


# Injection-check models offered in Settings (.env may name any)
INJECTION_MODELS = (FAST_MODEL, HELPER_MODEL)


def injection_check_on() -> bool:
    return _switch(preference("injection_check"), True)


def injection_model() -> str:
    return preference("injection_model") or DEFAULTS["injection_model"]


# Services are SERVICE_PREFIX.<name>: launchd labels on macOS, systemd user units (<label>.service) on Linux. Change
# the prefix only for a second install under the same user account.
SERVICE_NAMES = ("livekit", "agent", "web", "tasks")
SERVICE_PREFIX = os.environ.get("JARVIS_SERVICE_PREFIX", "").strip().strip(".") or "com.jarvis"


def service_label(name: str) -> str:
    """launchd label, or systemd unit name without .service, for a service in SERVICE_NAMES ("agent" ->
    com.jarvis.agent)."""
    return f"{SERVICE_PREFIX}.{name}"


# Local LiveKit server (deploy/livekit.yaml) and the phone web app
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "").strip() or "ws://127.0.0.1:7880"
WEB_PORT = int(os.environ.get("JARVIS_WEB_PORT", "") or "8787")
# Hashed name/passcode logins (jarvis.core.logins)
LOGINS_FILE = SECRETS_DIR / "logins.json"

# Google OAuth client normally comes from the vault; until set, this JSON file (or secrets/client_secret*.json) is used.
# One token per account label (jarvis.integrations.accounts).
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip() or next(
    (str(f) for f in sorted(SECRETS_DIR.glob("client_secret*.json"))), "")
