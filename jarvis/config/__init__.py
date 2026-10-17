"""Static settings, read once at import: the defaults in the checkout's config/*.toml, overridden by JARVIS_HOME's
config.toml, overridden by the environment (.env). Model ids come from config/models.toml (jarvis.config.models);
none is written in the code. Settings-panel preferences, which change while JARVIS runs, are read when used
(jarvis.state.preferences).

Set-but-empty counts as unset (the default). Task sessions blank every .env variable, and load_dotenv won't fill a
variable that is set, so code they start (e.g. the MCP launcher) sees defaults."""

import os
import pwd
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from jarvis.config import files
from jarvis.config.files import ROOT, ConfigError  # noqa: F401  (ROOT: the code checkout)
from jarvis.config.models import Models
from jarvis.core import desktop, platform

# Everything this install keeps (jarvis.config.files): config.toml, .env, data, secrets, models, logs
HOME_DIR = files.home()
load_dotenv(HOME_DIR / ".env")
# Vault key; popped from the environment so child processes never inherit it
MASTER_KEY = os.environ.pop("JARVIS_MASTER_KEY", "").strip()
SETTINGS = files.load(HOME_DIR)
MODELS = Models(SETTINGS["models"])

ON, OFF = ("1", "on", "true", "yes"), ("0", "off", "false", "no")


def _switch(value: str, default: bool) -> bool:
    value = value.strip().lower()
    return True if value in ON else False if value in OFF else default


def _number(value: str, default: float, low: float, high: float) -> float:
    try:
        return min(high, max(low, float(value)))
    except ValueError:
        return default


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def setting(key: str):
    """A value from the settings files, by dotted key ("tasks.max_turns")."""
    return files.value(SETTINGS, key)


def text(key: str, env: str = "") -> str:
    return (_env(env) if env else "") or str(setting(key))


def flag(key: str, env: str) -> bool:
    """On/off: the variable's 1/on/true/yes or 0/off/false/no, else the settings files."""
    default = setting(key)
    return _switch(os.environ.get(env, ""), _switch(default, False) if isinstance(default, str) else bool(default))


def number(key: str, env: str, low: float = float("-inf"), high: float = float("inf")) -> float:
    """A number from the variable, else the settings files, kept within low..high."""
    default = float(setting(key))
    return _number(_env(env) or str(default), default, low, high)


def whole(key: str, env: str, low: float = float("-inf"), high: float = float("inf")) -> int:
    return int(number(key, env, low, high))


def model_file(name: str) -> Path:
    """A model in MODELS_DIR, unless `name` is a full path."""
    return MODELS_DIR / Path(name).expanduser()


# Install data folder, also the owner's profile folder. Tests override JARVIS_DATA_DIR.
DATA_DIR = Path(_env("JARVIS_DATA_DIR") or HOME_DIR / "data")
SECRETS_DIR = HOME_DIR / "secrets"
LOGS_DIR = HOME_DIR / "logs"
MODELS_DIR = HOME_DIR / "models"
for _folder in (DATA_DIR, SECRETS_DIR):
    try:
        _folder.mkdir(parents=True, exist_ok=True)
    except FileExistsError:  # there but unreadable: a task's sandbox walls off the install's own folders
        pass
# Install database: profiles, plus the owner's tasks, to-dos, projects, accounts, settings and secrets
DB_FILE = Path(_env("JARVIS_DB_FILE") or DATA_DIR / "tasks.db")

# Settings-table key -> .env variable that overrides it. Name and time zone live on the profile (env wins for the
# owner). max_tasks is read at service start; voice, speed and speech gate per conversation; quiet hours (local
# hours with no watch checks or notices, jarvis.tasks.hold; "off" = never quiet) at each check. Read with
# jarvis.state.preferences.
PREFERENCES = {"name": "JARVIS_USER_NAME", "timezone": "JARVIS_TIMEZONE", "voice": "JARVIS_TTS_VOICE",
               "voice_speed": "JARVIS_TTS_SPEED", "speech_gate": "JARVIS_SPEECH_GATE", "max_tasks": "JARVIS_MAX_TASKS",
               "browser": "JARVIS_BROWSER", "browser_window": "JARVIS_BROWSER_WINDOW",
               "speaker_check": "JARVIS_SPEAKER_CHECK", "speaker_threshold": "JARVIS_SPEAKER_THRESHOLD",
               "injection_check": "JARVIS_INJECTION_CHECK", "injection_model": "JARVIS_INJECTION_MODEL",
               "quiet_hours": "JARVIS_QUIET_HOURS"}
# Fallbacks when neither is set; name and time zone fall back to mac_name() / mac_timezone()
DEFAULTS = {key: str(value) for key, value in setting("preferences").items()} | {
    "injection_model": MODELS.role("injection")}
SPEED_RANGE, MAX_TASKS_RANGE, SPEAKER_THRESHOLD_RANGE = (0.5, 2.0), (1, 12), (0.1, 0.8)


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
CLAUDE_CLI = os.path.expanduser(os.environ.get("JARVIS_CLAUDE_CLI", ""))
# Claude login: the user's own Claude Code sign-in, unless JARVIS_CLAUDE_CONFIG_DIR gives JARVIS one of its own
# (relative to HOME_DIR, e.g. secrets/claude). Only that one is passed to the CLI: setting CLAUDE_CONFIG_DIR at all,
# even to ~/.claude, makes the CLI look for a different macOS Keychain entry, so the user's own login wouldn't be found.
CLAUDE_OWN_CONFIG_DIR = (os.environ.get("JARVIS_CLAUDE_CONFIG_DIR", "")
                         and str(HOME_DIR / Path(os.environ["JARVIS_CLAUDE_CONFIG_DIR"]).expanduser()))
# The CLI's folder either way: its session transcripts, and on Linux the login
CLAUDE_CONFIG_DIR = (CLAUDE_OWN_CONFIG_DIR or os.environ.get("CLAUDE_CONFIG_DIR", "")
                     or str(Path.home() / ".claude"))
# Named Claude logins of JARVIS's own, one CLI config folder each, and which one it uses (jarvis.llm.claude_logins)
CLAUDE_LOGINS_DIR = SECRETS_DIR / "claude-logins"

# Models are asked for by role (jarvis.llm.roles; config/models.toml), never by a constant here
# Seconds one spoken turn may wait on file lookups in all; past that they carry on in the background, announced
LOOKUP_WAIT = number("voice_brain.lookup_wait", "JARVIS_LOOKUP_WAIT", 0.0, 60.0)
EFFORT = text("voice_brain.effort", "JARVIS_EFFORT")
# Background lookups handed off by the voice brain (jarvis.brain.side): time limit (file lookups too)
SIDE_MINUTES = number("voice_brain.side_minutes", "JARVIS_SIDE_MINUTES", 0.5, 30.0)
# A fast model picks when to speak updates and reminders (jarvis.voice.floor); off = as soon as nobody is talking
PROACTIVE_JUDGE = flag("voice_brain.proactive_judge", "JARVIS_PROACTIVE_JUDGE")
# CLI ENABLE_TOOL_SEARCH for MCP tools (JARVIS's own always load): true = always deferred, auto:N = deferred once they'd
# fill N% of context, false = all loaded up front. Deferring keeps many connections from slowing every reply.
TOOL_SEARCH = text("voice_brain.tool_search", "JARVIS_TOOL_SEARCH").lower()
# Seconds an MCP call may block a spoken reply before continuing in the background (jarvis.integrations.mcp_relay).
# 0 = always wait. Tasks always wait.
CONNECTION_WAIT = number("voice_brain.connection_wait", "JARVIS_CONNECTION_WAIT", 0.0, 600.0)
# Seconds one of JARVIS's own tools (a merge, a long read) may block a spoken reply before carrying on in the
# background, its result coming back in a brain turn of its own (jarvis.brain.kept). 0 = always wait. Tasks always wait.
TOOL_WAIT = number("voice_brain.tool_wait", "JARVIS_TOOL_WAIT", 0.0, 600.0)
MAX_TOOL_ROUNDS = whole("voice_brain.max_tool_rounds", "JARVIS_MAX_TOOL_ROUNDS")
# A spoken turn's own work (jarvis.brain.budget): past this many tool calls, or this many seconds, further calls are
# refused and the brain hands the rest to a lookup or a task, so the user can keep talking
TURN_TOOLS = whole("voice_brain.turn_tools", "JARVIS_TURN_TOOLS", 1)
TURN_SECONDS = number("voice_brain.turn_seconds", "JARVIS_TURN_SECONDS", 1.0, 600.0)
# How long a risky command awaits a spoken yes, and how long that yes stays valid
APPROVAL_MINUTES = whole("voice_brain.approval_minutes", "JARVIS_APPROVAL_MINUTES", 1)
# Replies the yes/no word lists can't read go to the consent role (jarvis.safety.consent); unclear after CONSENT_SECONDS.
# Keep CONSENT_SECONDS well above CLI startup time. JARVIS_CONSENT=off = word lists only.
CONSENT_ENABLED = flag("consent.enabled", "JARVIS_CONSENT")
CONSENT_SECONDS = number("consent.seconds", "JARVIS_CONSENT_SECONDS", 0.5, 15.0)
# Prompt-injection screen on tool results (jarvis.gates.injection). On/off and model are preferences. A check that
# times out passes the result through, marked unchecked.
INJECTION_VOICE_SECONDS = number("injection.voice_seconds", "JARVIS_INJECTION_VOICE_SECONDS", 1.0, 30.0)
INJECTION_TASK_SECONDS = number("injection.task_seconds", "JARVIS_INJECTION_TASK_SECONDS", 1.0, 120.0)


def main_checkout(root: Path | None = None) -> Path:
    """The checkout a git worktree belongs to (its .git file says "gitdir: <main>/.git/worktrees/<name>"); `root`
    itself if it's not a worktree. Task worktrees live in JARVIS_HOME's data/worktrees, which may be anywhere."""
    root = root or ROOT
    try:
        gitdir = Path((root / ".git").read_text().removeprefix("gitdir:").strip())
    except OSError:  # a folder (a main checkout), or no git at all
        return root
    return gitdir.parents[2] if gitdir.parent.name == "worktrees" and gitdir.parents[1].name == ".git" else root


def projects_home() -> Path:
    """Default projects folder: the parent of this checkout, or ~/Projects if that parent is $HOME or a disk root.

    Too broad a parent would let tasks read far too much. A task worktree resolves to its main checkout."""
    home = main_checkout().parent
    return Path("~/Projects").expanduser() if home in (Path.home(), Path(home.anchor)) else home


PROJECTS_HOME = projects_home()

# Task runner (jarvis.tasks.runner), separate from voice sessions
RUNNER_SOCKET = DATA_DIR / "runner.sock"
# project_register default location: NEW_PROJECTS_DIR/<name>
NEW_PROJECTS_DIR = Path(_env("JARVIS_NEW_PROJECTS_DIR") or PROJECTS_HOME).expanduser()
WORKTREES_DIR = DATA_DIR / "worktrees"
TASK_CACHE_DIR = DATA_DIR / "task-cache"  # build and package caches for sandboxed commands
TASK_EFFORT = text("tasks.effort", "JARVIS_TASK_EFFORT")
TASK_MAX_TURNS = whole("tasks.max_turns", "JARVIS_TASK_MAX_TURNS")
# Times a run that ran out of turns is carried on by itself per user ask (jarvis.tasks.runner); 0 = never
TASK_CONTINUES = whole("tasks.continues", "JARVIS_TASK_CONTINUES", 0, 20)
# How long a task's risky-command request waits for the user
TASK_APPROVAL_HOURS = number("tasks.approval_hours", "JARVIS_TASK_APPROVAL_HOURS", 1.0)
# Minutes without progress before a task is flagged stalled (jarvis.tasks.stall); 0 = never
TASK_STALL_MINUTES = whole("tasks.stall_minutes", "JARVIS_TASK_STALL_MINUTES", 0, 24 * 60)
# Tasks interrupted by a restart to resume on runner start; 0 = none
RESUME_TASKS = whole("tasks.resume", "JARVIS_RESUME_TASKS", 0, 12)
# Allow the Artifact tool (claude.ai pages; needs a subscription login, not an API key) for code_change and research
# tasks, others when granted (jarvis.tasks.access). 0 = off for all.
TASK_ARTIFACTS = flag("tasks.artifacts", "JARVIS_TASK_ARTIFACTS")
# Read-only folders for code tasks (secrets inside stay hidden). Set but empty = none.
TASK_READ_DIRS = [
    Path(d.strip()).expanduser()
    for d in os.environ.get("JARVIS_TASK_READ_DIRS", str(PROJECTS_HOME)).split(",") if d.strip()
]
# Goal loop (jarvis.tasks.goal): a "finished" task is checked against its request and sent back up to GOAL_ROUNDS times.
# GOAL_MAX_COST: CLI-reported dollars after which it stops (0 = no limit). GOAL_DAILY_CHECKS: cap per 24 h, all tasks.
GOAL_LOOP = flag("goal.loop", "JARVIS_GOAL_LOOP")
GOAL_ROUNDS = whole("goal.rounds", "JARVIS_GOAL_ROUNDS", 0, 5)
GOAL_MAX_COST = number("goal.max_cost", "JARVIS_GOAL_MAX_COST", 0.0, 10000.0)
GOAL_DAILY_CHECKS = whole("goal.daily_checks", "JARVIS_GOAL_DAILY_CHECKS", 1, 10000)
# Task-end announcement (jarvis.tasks.announce), worded by the announce role; fixed wording on timeout or when off
ANNOUNCE_ENABLED = flag("announce.enabled", "JARVIS_ANNOUNCE")
ANNOUNCE_SECONDS = number("announce.seconds", "JARVIS_ANNOUNCE_SECONDS", 5.0, 120.0)

# Other machines (jarvis.node). Where jobs run is a per-user setting (Settings › Machines); when it lets JARVIS choose,
# the placement role picks among the machines online. A machine unreachable for NODE_LOST_MINUTES while it has work is
# reported once; a task whose session lives on a machine waits NODE_WAIT_MINUTES for it to come back. Copies of a
# project sent to a machine stop at NODE_SYNC_MAX_GB.
NODE_LOST_MINUTES = number("node.lost_minutes", "JARVIS_NODE_LOST_MINUTES", 1, 24 * 60)
NODE_WAIT_MINUTES = number("node.wait_minutes", "JARVIS_NODE_WAIT_MINUTES", 0, 24 * 60)
NODE_SYNC_MAX_GB = number("node.sync_max_gb", "JARVIS_NODE_SYNC_MAX_GB", 0.1, 10000)

# Notices (jarvis.tasks.notices): JARVIS's own news, said unasked: tasks stuck or waiting, usage limits, machines lost
NOTICES_ENABLED = flag("notices.enabled", "JARVIS_NOTICES")

# Scheduler (jarvis.tasks.scheduler): reminders, repeating tasks and watches
SCHEDULER_ENABLED = flag("scheduler.enabled", "JARVIS_SCHEDULER")
SCHEDULE_EFFORT = text("scheduler.effort", "JARVIS_SCHEDULE_EFFORT")
# Plan usage last reported by the CLI (jarvis.llm.usage). Scheduled model runs wait for the 5-hour reset once
# USAGE_HOLD of it is used (1 = never), and for any window that is exhausted.
USAGE_FILE = DATA_DIR / "usage.json"
USAGE_HOLD = number("usage.hold", "JARVIS_USAGE_HOLD", 0.5, 1.0)

# Learning review (jarvis.tasks.learning): after a conversation ends (hang-up or quiet), suggest memory edits
LEARNING_ENABLED = flag("learning.enabled", "JARVIS_LEARNING")
LEARNING_QUIET_MINUTES = whole("learning.quiet_minutes", "JARVIS_LEARNING_QUIET_MINUTES", 1)

# Repeated to-dos (jarvis.state.todo): a new item sharing most of its words with ones on the list goes to
# the todo_match role, which says whether it repeats one; on failure or after TODO_MATCH_SECONDS it's added. The voice
# tool waits for it, so keep it short, though well above CLI startup time. JARVIS_TODO_MATCH=off = only items worded
# the same count as repeats.
TODO_MATCH_ENABLED = flag("todo_match.enabled", "JARVIS_TODO_MATCH")
TODO_MATCH_SECONDS = number("todo_match.seconds", "JARVIS_TODO_MATCH_SECONDS", 2.0, 30.0)

# Skills (jarvis.skills): Claude Code SKILL.md folders. Scripts in installed skills are reviewed before enabling.

# Web Push (jarvis.notify.push): alerts and task updates to the home-screen web app
PUSH_ENABLED = flag("push.enabled", "JARVIS_PUSH")  # sends nothing until a device subscribes
# VAPID "sub" claim (mailto: or https:); unset = default Google account's address
PUSH_CONTACT = text("push.contact", "JARVIS_PUSH_CONTACT")
VAPID_KEY_FILE = SECRETS_DIR / "vapid_private.pem"  # generated on first use
# APNs (jarvis.notify.apns) for the iOS app, whose web view lacks Web Push. Key from developer.apple.com > Keys;
# its key id is in the vault.
APNS_KEY_FILE = SECRETS_DIR / "apns_key.p8"
APNS_TEAM_ID = text("apns.team_id", "JARVIS_APNS_TEAM_ID")  # Membership details
# FCM (jarvis.notify.fcm) for the Android app: a Firebase service-account key (Project settings > Service accounts)
FCM_SERVICE_ACCOUNT_FILE = SECRETS_DIR / "fcm_service_account.json"

# On-device voice: Whisper in, Kokoro out. Whisper runs on MLX on Apple silicon, else on faster-whisper
# (jarvis.media.whisper). JARVIS_STT names the model: on MLX 4-bit uses ~0.5 GB vs 1.6 GB at 16-bit (matters on 8 GB);
# empty elsewhere = whisper_gpu on an NVIDIA GPU, whisper_cpu on the CPU.
STT_MODEL = text("speech.whisper_mlx" if platform.APPLE_SILICON else "speech.whisper", "JARVIS_STT")
WHISPER_GPU_MODEL = text("speech.whisper_gpu")
WHISPER_CPU_MODEL = text("speech.whisper_cpu")  # live voice: English only
WHISPER_CPU_FILES_MODEL = text("speech.whisper_cpu_files")  # recordings: any language
# faster-whisper's device: cuda or cpu; empty = cuda when an NVIDIA GPU is usable
STT_DEVICE = text("speech.device", "JARVIS_STT_DEVICE").lower()
# Recordings (jarvis.media.transcribe) can be in any language: empty off Apple silicon = a multilingual default
TRANSCRIBE_MODEL = text("speech.transcribe", "JARVIS_TRANSCRIBE_MODEL") or (STT_MODEL if platform.APPLE_SILICON else "")
# Prompt Whisper with the speech vocabulary (jarvis.state.vocabulary); off = only fix known mishearings
STT_PROMPT = flag("speech.prompt", "JARVIS_STT_PROMPT")
# Warm spare job processes so calls connect instantly (~300 MB each; models live in the shared speech process).
# 0 saves the memory; a call then waits a few seconds.
VOICE_IDLE_PROCESSES = whole("voice.idle_processes", "JARVIS_VOICE_IDLE_PROCESSES", 0)
# MLX buffer-cache cap in MB (freed Whisper buffers, ~0.7 GB per clip uncapped). 256 costs nothing measurable;
# 64 saves 0.2 GB for ~25 ms/clip; 0 = free after every clip; -1 = no cap. Apple silicon only.
MLX_CACHE_MB = whole("voice.mlx_cache_mb", "JARVIS_MLX_CACHE_MB")
# Kokoro ONNX memory arena: never shrinks (~1 GB after a 20 s sentence) and gave no speedup, so off by default
TTS_MEMORY_ARENA = flag("voice.tts_memory_arena", "JARVIS_TTS_MEMORY_ARENA")
# Allocator caching of large freed blocks in voice child processes (macOS's large cache, glibc's growing mmap
# threshold). Off so freed memory returns to the system.
VOICE_MALLOC_LARGE_CACHE = flag("voice.malloc_large_cache", "JARVIS_VOICE_MALLOC_LARGE_CACHE")
# Minutes between voice-process memory logs (jarvis.voice.ram); 0 = never
VOICE_MEMORY_LOG_MINUTES = number("voice.memory_log_minutes", "JARVIS_VOICE_MEMORY_LOG_MINUTES", 0.0)
# Silence in seconds after the last speech that ends a turn. Counted from when speech stopped, so the VAD's silence
# (VAD_MIN_SILENCE, its floor) and Whisper's time fall inside it; the turn also waits for Whisper's words
# (jarvis.voice.turn.Transcribed). Lower answers sooner but cuts off more mid-thought pauses (a turn that goes on
# before the reply is heard is answered together with the next).
TURN_SILENCE = number("voice.turn_silence", "JARVIS_TURN_SILENCE", 0.3, 5.0)
# Below this average log-prob a turn is marked unsure, so the brain may confirm its guess
WHISPER_UNSURE_LOGPROB = number("voice.whisper_unsure_logprob", "JARVIS_WHISPER_UNSURE_LOGPROB", -10.0, 0.0)
# Silero VAD: speech needed to start an utterance and the probability threshold. LiveKit's 0.05 s default lets a
# click through and Whisper hears a phantom "Thank you.". Barge-in still needs 0.5 s.
VAD_MIN_SPEECH = number("voice.vad_min_speech", "JARVIS_VAD_MIN_SPEECH")
# Silence that ends an utterance (LiveKit default). Floor on every reply's latency; lower = more mid-sentence cuts.
VAD_MIN_SILENCE = number("voice.vad_min_silence", "JARVIS_VAD_MIN_SILENCE", 0.3, 2.0)
VAD_THRESHOLD = number("voice.vad_threshold", "JARVIS_VAD_THRESHOLD")
VOICE_TURNS_LOG = LOGS_DIR / "voice-turns.jsonl"  # per-stage turn timing

# Kokoro model (name in MODELS_DIR or full path); first existing fallback if missing. 32-bit by default: int8 saves
# ~230 MB but renders 30-60% slower on an M2, leaving pauses between sentences under load.
TTS_MODEL = model_file(text("speech.kokoro", "JARVIS_TTS_MODEL"))
TTS_MODEL_FALLBACKS = tuple(model_file(name) for name in setting("speech.kokoro_fallbacks"))
TTS_VOICES_FILE = model_file(text("speech.kokoro_voices"))
# Kokoro threads. Empty = performance cores - 1, clamped to 2..4 (as fast as 8 under load, frees a core for the
# call); 0 = ONNX Runtime default.
TTS_THREADS = text("speech.tts_threads", "JARVIS_TTS_THREADS")
# Audio files (jarvis.media.audio_file): text-to-file in a separate low-priority Kokoro process, leaving cores for the
# live voice. AUDIO_MODEL falls back to TTS_MODEL. AUDIO_THREADS 0 = a quarter of the cores, at least 2.
AUDIO_DIR = Path(_env("JARVIS_AUDIO_DIR") or desktop.user_dir("DOWNLOAD", "Downloads")).expanduser()
AUDIO_FORMATS = ("m4a", "mp3", "wav")
AUDIO_FORMAT = text("audio_files.format", "JARVIS_AUDIO_FORMAT").lower().removeprefix(".")
if AUDIO_FORMAT not in AUDIO_FORMATS:
    AUDIO_FORMAT = "m4a"
AUDIO_MODEL = model_file(text("audio_files.model", "JARVIS_AUDIO_MODEL"))
AUDIO_THREADS = whole("audio_files.threads", "JARVIS_AUDIO_THREADS", 0)  # 0 = auto
# Mic noise suppression (jarvis.voice.denoise, DeepFilterNet 3 on CPU). on = filtered everywhere; vad = VAD only,
# Whisper hears the original; off = never
DENOISE_MODES = ("on", "vad", "off")
_denoise = text("voice.denoise", "JARVIS_DENOISE").lower()
DENOISE = {"1": "on", "true": "on", "yes": "on", "0": "off", "false": "off", "no": "off"}.get(_denoise, _denoise)
if DENOISE not in DENOISE_MODES:
    DENOISE = "on"
# Max attenuation in dB, so speech mistaken for noise stays audible; 0 = no limit
DENOISE_ATTEN_DB = number("voice.denoise_atten_db", "JARVIS_DENOISE_ATTEN_DB", 0.0)
DENOISE_MODEL = model_file(text("speech.denoise"))
# Speech gate (jarvis.voice.speech_gate): drops clips without real speech, judged on audio; known Whisper phantoms
# face a higher bar. shadow = log only; enforce = drop; off = no checks. The mode is a preference.
SPEECH_GATE_MODES = ("shadow", "enforce", "off")


def _gate(key: str) -> float:
    return number(f"speech_gate.{key}", f"JARVIS_SPEECH_GATE_{key.upper()}")


SPEECH_GATE_MIN_SPEECH_SECONDS = _gate("min_speech_seconds")
SPEECH_GATE_MIN_SECONDS = _gate("min_seconds")
SPEECH_GATE_MIN_DBFS = _gate("min_dbfs")
SPEECH_GATE_NO_SPEECH = _gate("no_speech")
SPEECH_GATE_WEAK_SPEECH_SECONDS = _gate("weak_speech_seconds")
# Faint: little speech AND quiet AND short (phantoms that pass the checks above)
SPEECH_GATE_FAINT_SPEECH_SECONDS = _gate("faint_speech_seconds")
SPEECH_GATE_FAINT_DBFS = _gate("faint_dbfs")
SPEECH_GATE_FAINT_MAX_SECONDS = _gate("faint_max_seconds")
# A transcript that is only a known phantom ("Thank you.", "Bye.") counts only if loud enough, long enough and
# confident. "yes"/"no"/"okay" are never on the list. JARVIS_SPEECH_GATE_PHANTOMS (comma-separated) replaces it.
SPEECH_GATE_PHANTOM_DBFS = _gate("phantom_dbfs")
SPEECH_GATE_PHANTOM_SPEECH_SECONDS = _gate("phantom_speech_seconds")
SPEECH_GATE_PHANTOMS = ([p.strip() for p in _env("JARVIS_SPEECH_GATE_PHANTOMS").split(",") if p.strip()]
                        or list(setting("speech_gate.phantoms")))
# Unsure: avg log-prob below Whisper's retry threshold AND little speech
SPEECH_GATE_MIN_LOGPROB = _gate("min_logprob")
SPEECH_GATE_UNSURE_SPEECH_SECONDS = _gate("unsure_speech_seconds")
# Repetitive: gzip ratio over Whisper's threshold, i.e. a looping hallucination
SPEECH_GATE_MAX_COMPRESSION = _gate("max_compression")
SPEECH_GATE_LOG = LOGS_DIR / "speech-gate.jsonl"  # every clip measured, kept or not


def speech_gate_save_audio(mode: str) -> bool:
    """Save each judged clip as WAV; on by default in shadow mode."""
    return _switch(os.environ.get("JARVIS_SPEECH_GATE_SAVE_AUDIO", ""), mode == "shadow")


# Save short clips that pass even with SAVE_AUDIO off, to catch phantoms that slip through
SPEECH_GATE_SAVE_SHORT = flag("speech_gate.save_short", "JARVIS_SPEECH_GATE_SAVE_SHORT")
SPEECH_GATE_SHORT_SECONDS = _gate("short_seconds")
SPEECH_GATE_SHORT_SPEECH_SECONDS = _gate("short_speech_seconds")
SPEECH_GATE_AUDIO_DIR = LOGS_DIR / "speech-gate-audio"
SPEECH_GATE_AUDIO_KEEP = whole("speech_gate.audio_keep", "JARVIS_SPEECH_GATE_AUDIO_KEEP", 1)  # newest files kept

# Speaker verification (jarvis.voice.speaker) against the enrolled voiceprint; drops other voices (TV, others).
# on = drop; shadow = log only; off = no checks. No effect until a voiceprint exists. Mode and threshold are
# preferences.
SPEAKER_MODES = ("on", "shadow", "off")
SPEAKER_MODEL = model_file(text("speech.speaker"))  # `make models`; voiceprints record their model
SPEAKER_MODEL_ID = text("speech.speaker_id")


def _speaker(key: str, low: float = 0.0, high: float = 1e6) -> float:
    return number(f"speaker.{key}", f"JARVIS_SPEAKER_{key.upper()}", low, high)


# Short utterances are less telling: threshold - SHORT_MARGIN at <= SHORT_SECONDS, rising to full at LONG_SECONDS.
# Mid-conversation (user heard within RECENT_SECONDS, or a reply ended within REPLY_SECONDS) subtract RELAX too,
# and keep anything under MIN_SECONDS regardless, so a quick "yes" isn't lost.
SPEAKER_SHORT_MARGIN = _speaker("short_margin", 0.0, 1.0)
SPEAKER_RELAX = _speaker("relax", 0.0, 1.0)
SPEAKER_SHORT_SECONDS = _speaker("short_seconds")
SPEAKER_LONG_SECONDS = _speaker("long_seconds")
SPEAKER_MIN_SECONDS = _speaker("min_seconds")
SPEAKER_RECENT_SECONDS = _speaker("recent_seconds")
SPEAKER_REPLY_SECONDS = _speaker("reply_seconds")
# Use clearly verified utterances (long, ADAPT_MARGIN over threshold) as extra references for this call's mic/room
SPEAKER_ADAPT = flag("speaker.adapt", "JARVIS_SPEAKER_ADAPT")
SPEAKER_ADAPT_MARGIN = _speaker("adapt_margin", 0.0, 1.0)
# Enrollment: target speech (silence excluded), minimum accepted, and spoken-enrollment timeout
SPEAKER_ENROLL_SECONDS = _speaker("enroll_seconds", 5.0, 300.0)
SPEAKER_ENROLL_MIN_SECONDS = _speaker("enroll_min_seconds", 3.0, 300.0)
SPEAKER_ENROLL_TIMEOUT = _speaker("enroll_timeout", 10.0, 3600.0)
SPEAKER_LOG = LOGS_DIR / "speaker-check.jsonl"  # score, threshold, verdict per utterance
# Barge-in (jarvis.voice.barge_in) only for the verified user: first judged at MIN_SECONDS of speech, then every STEP
# on the latest MAX_SECONDS until it matches or stops. off = any voice interrupts.
SPEAKER_BARGE_IN = flag("speaker.barge_in", "JARVIS_SPEAKER_BARGE_IN")
SPEAKER_BARGE_IN_MIN_SECONDS = _speaker("barge_in_min_seconds", 0.2, 5.0)
SPEAKER_BARGE_IN_MAX_SECONDS = max(SPEAKER_BARGE_IN_MIN_SECONDS, _speaker("barge_in_max_seconds", 0.2, 6.0))
SPEAKER_BARGE_IN_STEP = _speaker("barge_in_step", 0.05, 2.0)

# Browser (jarvis.integrations.browser): Playwright-driven Chrome with its own profile per user. Whether its tools are
# on and headed/headless are preferences.
BROWSER_WINDOWS = ("headed", "headless")

# Injection-check models offered in Settings (.env may name any)
INJECTION_MODELS = MODELS.choices("injection")

# Services are SERVICE_PREFIX.<name>: launchd labels on macOS, systemd user units (<label>.service) on Linux. Change
# the prefix only for a second install under the same user account.
SERVICE_NAMES = ("livekit", "agent", "web", "tasks")
SERVICE_PREFIX = text("services.prefix", "JARVIS_SERVICE_PREFIX").strip(".") or "com.jarvis"


def service_label(name: str) -> str:
    """launchd label, or systemd unit name without .service, for a service in SERVICE_NAMES ("agent" ->
    com.jarvis.agent)."""
    return f"{SERVICE_PREFIX}.{name}"


# Local LiveKit server (deploy/livekit.yaml) and the phone web app
LIVEKIT_URL = text("services.livekit_url", "LIVEKIT_URL")
WEB_PORT = whole("services.web_port", "JARVIS_WEB_PORT")
# Hashed name/passcode logins (jarvis.auth.logins)
LOGINS_FILE = SECRETS_DIR / "logins.json"

# Google OAuth client normally comes from the vault; until set, this JSON file (or secrets/client_secret*.json) is used.
# One token per account label (jarvis.integrations.accounts).
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip() or next(
    (str(f) for f in sorted(SECRETS_DIR.glob("client_secret*.json"))), "")
