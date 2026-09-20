"""Settings, read once from the environment (.env in the project root) and, for the preferences the Settings panel
changes, from the database's settings table (jarvis.state.settings). A preference's variable in .env wins over the table.

A setting set but empty counts as unset (its default), except where an empty value means something: task sessions
blank every .env setting (jarvis.tasks.worker), and JARVIS's own code they start, such as the MCP servers' launcher,
loads this module with them blank. load_dotenv won't fill a variable that is set, even to nothing."""

import os
import pwd
import sqlite3
from contextlib import closing
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")
# The key to the secrets in the database (jarvis.core.vault), taken out of the environment here, so nothing this process
# starts inherits it
MASTER_KEY = os.environ.pop("JARVIS_MASTER_KEY", "").strip()


ON, OFF = ("1", "on", "true", "yes"), ("0", "off", "false", "no")


def _switch(value: str, default: bool) -> bool:
    value = value.strip().lower()
    return True if value in ON else False if value in OFF else default


def _flag(name: str, default: bool) -> bool:
    """An on/off setting: 1, on, true or yes; 0, off, false or no; anything else (or unset) is the default."""
    return _switch(os.environ.get(name, ""), default)


# The install's data folder, which is also the owner's (jarvis.core.profile: each person's data folder, database and
# sign-ins are their profile's). JARVIS_DATA_DIR puts it elsewhere: the tests use one of their own.
DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", "") or ROOT / "data")
SECRETS_DIR = ROOT / "secrets"
DATA_DIR.mkdir(parents=True, exist_ok=True)
SECRETS_DIR.mkdir(exist_ok=True)
# The install's database, holding the profiles, and the owner's: tasks, to-dos, projects, accounts, settings, secrets.
# JARVIS_DB_FILE puts it elsewhere.
DB_FILE = Path(os.environ.get("JARVIS_DB_FILE", "") or DATA_DIR / "tasks.db")

# The preferences the Settings panel changes, by their key in the settings table, each with the variable that wins
# over it from .env. Name and time zone are the profile's instead (jarvis.core.profile), read as they're used, their
# variables winning for the owner's; the task limit is read when a service starts; voice, speed and the speech gate at
# the start of each conversation (preference() reads the table afresh).
PREFERENCES = {"name": "JARVIS_USER_NAME", "timezone": "JARVIS_TIMEZONE", "voice": "JARVIS_TTS_VOICE",
               "voice_speed": "JARVIS_TTS_SPEED", "speech_gate": "JARVIS_SPEECH_GATE", "max_tasks": "JARVIS_MAX_TASKS",
               "browser": "JARVIS_BROWSER", "browser_window": "JARVIS_BROWSER_WINDOW",
               "speaker_check": "JARVIS_SPEAKER_CHECK", "speaker_threshold": "JARVIS_SPEAKER_THRESHOLD",
               "injection_check": "JARVIS_INJECTION_CHECK", "injection_model": "JARVIS_INJECTION_MODEL"}
# With neither: these, and for name and time zone the Mac's own (mac_name(), mac_timezone())
DEFAULTS = {"voice": "bm_george", "voice_speed": "1.05", "speech_gate": "enforce", "max_tasks": "6",
            "browser": "on", "browser_window": "headed", "speaker_check": "on", "speaker_threshold": "0.35",
            "injection_check": "on", "injection_model": "claude-haiku-4-5-20251001"}
SPEED_RANGE, MAX_TASKS_RANGE, SPEAKER_THRESHOLD_RANGE = (0.5, 2.0), (1, 12), (0.1, 0.8)


def stored(key: str, db_file: Path | None = None) -> str:
    """A preference as the settings table has it, in `db_file` or the current profile's database (jarvis.core.profile):
    "" when it isn't there, or the database is missing or unreadable."""
    if db_file is None:
        from jarvis.core import profile  # which imports this
        db_file = profile.current().db
    try:
        with closing(sqlite3.connect(f"file:{db_file}?mode=ro", uri=True)) as db:
            row = db.execute("select value from settings where key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return ""
    return row[0].strip() if row else ""


def preference(key: str) -> str:
    """A preference from .env, else from the settings table; "" when neither has it, for the caller's default."""
    return os.environ.get(PREFERENCES[key], "").strip() or stored(key)


def _number(value: str, default: float, low: float, high: float) -> float:
    try:
        return min(high, max(low, float(value)))
    except ValueError:
        return default


def mac_name() -> str:
    """The first name on the Mac's user account, "Sam" from "Sam Lee"; "" when it has none."""
    try:
        full = pwd.getpwuid(os.getuid()).pw_gecos
    except KeyError:
        return ""
    return full.split(",")[0].split()[0] if full.strip() else ""


def mac_timezone() -> str:
    """The Mac's own time zone, from where /etc/localtime points (.../zoneinfo/Asia/Kolkata); "" when it can't tell."""
    _, found, zone = os.path.realpath("/etc/localtime").partition("/zoneinfo/")
    return zone if found and zone_ok(zone) else ""


def zone_ok(zone: str) -> bool:
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


# Who JARVIS works for, their name and time zone, is their profile (jarvis.core.profile); what JARVIS knows about them,
# their "about you" text, is a setting in their database (jarvis.state.settings).
# The name JARVIS answers to, as the safety scan (jarvis.safety.scan) knows it
ASSISTANT_NAME = "JARVIS"

# Empty = the CLI bundled with claude-agent-sdk, so its version moves with the SDK rather than the everyday install
CLAUDE_CLI = os.environ.get("JARVIS_CLAUDE_CLI", "")
CLAUDE_CONFIG_DIR = os.environ.get("JARVIS_CLAUDE_CONFIG_DIR", "") or str(SECRETS_DIR / "claude")
DEFAULT_MODEL = "claude-opus-5-5"
CLI_MODEL = os.environ.get("JARVIS_CLI_MODEL", "") or DEFAULT_MODEL
FAST_MODEL = "claude-haiku-4-5-20251001"  # subagents that only look things up
HELPER_MODEL = "claude-sonnet-5"  # subagents that need more judgment: test runs, web research
# The CLI brain's read-only lookup subagent, which keeps file contents out of the spoken turn
LOOKUP_MODEL = os.environ.get("JARVIS_LOOKUP_MODEL", "") or FAST_MODEL
EFFORT = os.environ.get("JARVIS_EFFORT", "") or "low"  # low keeps spoken replies fast
# A lighter model for voice turns a fast model judges need no tools and little thought (jarvis.brain.route), such as
# claude-sonnet-5; empty (the default) keeps every turn on CLI_MODEL
VOICE_QUICK_MODEL = os.environ.get("JARVIS_VOICE_QUICK_MODEL", "")
# Background lookups the voice brain hands off so the conversation carries on (jarvis.brain.side): their model, and
# how long one may take
SIDE_MODEL = os.environ.get("JARVIS_SIDE_MODEL", "") or HELPER_MODEL
SIDE_MINUTES = _number(os.environ.get("JARVIS_SIDE_MINUTES", ""), 3.0, 0.5, 30.0)
# Whether a fast model judges the moment to bring up task updates, alerts and reminders (jarvis.voice.floor); off says
# each as soon as nobody is talking
PROACTIVE_JUDGE = _flag("JARVIS_PROACTIVE_JUDGE", True)
# Connections' tools (MCP servers: jarvis.integrations.mcp_servers) stay out of a session's context until the model looks
# them up with the CLI's ToolSearch, so adding connections doesn't slow every reply; JARVIS's own tools always load.
# The CLI's ENABLE_TOOL_SEARCH value: true defers them always, auto:N only once they'd fill N% of the context window,
# false loads them all up front, as before.
TOOL_SEARCH = (os.environ.get("JARVIS_TOOL_SEARCH", "") or "true").strip().lower()
# How many seconds a connection's call may hold up a spoken reply: one still running then carries on as a background
# task, said when it ends (jarvis.integrations.mcp_relay). 0 waits for every call, as before. Tasks always wait.
CONNECTION_WAIT = _number(os.environ.get("JARVIS_CONNECTION_WAIT", ""), 20.0, 0.0, 600.0)
MAX_TOOL_ROUNDS = int(os.environ.get("JARVIS_MAX_TOOL_ROUNDS", "") or "8")
# How long a risky shell command stays open for a spoken yes, and a yes stays good for running it
APPROVAL_MINUTES = max(1, int(os.environ.get("JARVIS_APPROVAL_MINUTES", "") or "5"))
# A spoken reply the yes and no word lists can't read goes to one short call to CONSENT_MODEL (jarvis.safety.consent),
# which gives up after CONSENT_SECONDS and leaves it unclear; JARVIS_CONSENT=off keeps to the word lists. 3 seconds
# was too short: a CLI started for the call took longer than that alone, so every check ran out of time (2026-10-09).
CONSENT_ENABLED = _flag("JARVIS_CONSENT", True)
CONSENT_MODEL = os.environ.get("JARVIS_CONSENT_MODEL", "") or FAST_MODEL
CONSENT_SECONDS = _number(os.environ.get("JARVIS_CONSENT_SECONDS", ""), 8.0, 0.5, 15.0)
# What tools bring in (mail, pages, documents, files, connections' results) is screened for hidden instructions by a
# short call to a fast model before the model reading it acts on it (jarvis.safety.injection). Whether it's on, and on
# which model, are preferences, read as each session starts (injection_check_on, injection_model). A check that takes
# longer than this passes the result on with a note that it went unchecked: the voice brain waits less than tasks.
INJECTION_VOICE_SECONDS = _number(os.environ.get("JARVIS_INJECTION_VOICE_SECONDS", ""), 6.0, 1.0, 30.0)
INJECTION_TASK_SECONDS = _number(os.environ.get("JARVIS_INJECTION_TASK_SECONDS", ""), 30.0, 1.0, 120.0)


def projects_home() -> Path:
    """Where the user's projects live, unless .env says (JARVIS_NEW_PROJECTS_DIR, JARVIS_TASK_READ_DIRS; setup asks):
    the folder JARVIS's own checkout is in, as projects are usually kept side by side, or ~/Projects when that's the
    home folder itself or the top of the disk, which would let tasks read far too much. A task's worktree
    (data/worktrees/<name>) counts as the main checkout it was made from."""
    root = ROOT.parents[2] if ROOT.parent.name == "worktrees" and ROOT.parent.parent.name == "data" else ROOT
    home = root.parent
    return Path("~/Projects").expanduser() if home in (Path.home(), Path(home.anchor)) else home


PROJECTS_HOME = projects_home()

# Background tasks, run in parallel by the task runner (jarvis.tasks.runner), apart from any voice session
RUNNER_SOCKET = DATA_DIR / "runner.sock"
# Where project_register puts a new project when no folder is given: NEW_PROJECTS_DIR/<name>
NEW_PROJECTS_DIR = Path(os.environ.get("JARVIS_NEW_PROJECTS_DIR", "") or PROJECTS_HOME).expanduser()
WORKTREES_DIR = DATA_DIR / "worktrees"
TASK_CACHE_DIR = DATA_DIR / "task-cache"  # build and package caches for sandboxed commands
MAX_TASKS = int(_number(preference("max_tasks"), float(DEFAULTS["max_tasks"]), *MAX_TASKS_RANGE))  # running at once; the rest queue
TASK_MODEL = os.environ.get("JARVIS_TASK_MODEL", "") or DEFAULT_MODEL
TASK_EFFORT = os.environ.get("JARVIS_TASK_EFFORT", "") or "high"
TASK_MAX_TURNS = int(os.environ.get("JARVIS_TASK_MAX_TURNS", "") or "150")
# Models for a task's subagents: the read-only code searcher, and the test runner and web researcher
TASK_SEARCH_MODEL = os.environ.get("JARVIS_TASK_SEARCH_MODEL", "") or FAST_MODEL
TASK_HELPER_MODEL = os.environ.get("JARVIS_TASK_HELPER_MODEL", "") or HELPER_MODEL
# How long a task's request to run a risky command on the Mac waits for the user's answer
TASK_APPROVAL_HOURS = max(1.0, float(os.environ.get("JARVIS_TASK_APPROVAL_HOURS", "") or "12"))
# A running task with no sign of progress for this many minutes is flagged as stalled (jarvis.tasks.stall); 0 never
TASK_STALL_MINUTES = int(_number(os.environ.get("JARVIS_TASK_STALL_MINUTES", ""), 15, 0, 24 * 60))
# How many tasks a restart cut off are resumed, in their own sessions, when the runner starts again; 0 none
RESUME_TASKS = int(_number(os.environ.get("JARVIS_RESUME_TASKS", ""), 3, 0, 12))
# code_change and research tasks may publish pages to claude.ai (claude.ai/code/artifact/...) with Claude Code's Artifact
# tool, which only works on a claude.ai subscription login, not an API key; other tasks when granted it
# (jarvis.tasks.access). JARVIS_TASK_ARTIFACTS=0 turns it off for every task.
TASK_ARTIFACTS = _flag("JARVIS_TASK_ARTIFACTS", True)
# Folders every code task may read, but not change (secrets inside them stay hidden). Set but empty: none.
TASK_READ_DIRS = [
    Path(d.strip()).expanduser()
    for d in os.environ.get("JARVIS_TASK_READ_DIRS", str(PROJECTS_HOME)).split(",") if d.strip()
]
# The goal loop (jarvis.tasks.goal): a task that says it's finished is checked against what it was asked by one short
# call to GOAL_MODEL (unset, the tasks' own model, TASK_MODEL), and sent back to finish what it can, at most GOAL_ROUNDS
# times per request. GOAL_MAX_COST, in the CLI's reported dollars, stops sending back a task that has cost that much; 0,
# the default, is no limit (on a Claude subscription the figure is notional). GOAL_DAILY_CHECKS caps the checks across all
# tasks in any 24 hours.
GOAL_LOOP = _flag("JARVIS_GOAL_LOOP", True)
GOAL_MODEL = os.environ.get("JARVIS_GOAL_MODEL", "").strip() or TASK_MODEL
GOAL_ROUNDS = int(_number(os.environ.get("JARVIS_GOAL_ROUNDS", ""), 2, 0, 5))
GOAL_MAX_COST = _number(os.environ.get("JARVIS_GOAL_MAX_COST", ""), 0.0, 0.0, 10000.0)
GOAL_DAILY_CHECKS = int(_number(os.environ.get("JARVIS_GOAL_DAILY_CHECKS", ""), 100, 1, 10000))
# How a task's end is said (jarvis.tasks.announce): worded from its facts by one short call to ANNOUNCE_MODEL, which
# gives up after ANNOUNCE_SECONDS; then, or with JARVIS_ANNOUNCE=off, the same facts are said in fixed words
ANNOUNCE_ENABLED = _flag("JARVIS_ANNOUNCE", True)
ANNOUNCE_MODEL = os.environ.get("JARVIS_ANNOUNCE_MODEL", "") or FAST_MODEL
ANNOUNCE_SECONDS = _number(os.environ.get("JARVIS_ANNOUNCE_SECONDS", ""), 30.0, 5.0, 120.0)

# Heartbeat (jarvis.tasks.heartbeat): the task runner wakes every few minutes and flags what's new, read-only
HEARTBEAT_ENABLED = _flag("JARVIS_HEARTBEAT", True)
HEARTBEAT_MINUTES = max(1, int(os.environ.get("JARVIS_HEARTBEAT_MINUTES", "") or "15"))
HEARTBEAT_EVENT_MINUTES = max(1, int(os.environ.get("JARVIS_HEARTBEAT_EVENT_MINUTES", "") or "30"))  # "starting soon"
HEARTBEAT_QUIET_HOURS = os.environ.get("JARVIS_HEARTBEAT_QUIET_HOURS", "23:00-08:00")  # local time; empty = never quiet
HEARTBEAT_TRIAGE = _flag("JARVIS_HEARTBEAT_TRIAGE", False)
HEARTBEAT_MODEL = os.environ.get("JARVIS_HEARTBEAT_MODEL", "") or CLI_MODEL  # only used when triage is on

# Scheduler (jarvis.tasks.scheduler): reminders, repeating background tasks and "watch this for me" loops, in the runner
SCHEDULER_ENABLED = _flag("JARVIS_SCHEDULER", True)
# Scheduled runs and watch checks are routine, so they run on a lighter model than tasks started by hand (code_change
# runs keep TASK_MODEL)
SCHEDULE_MODEL = os.environ.get("JARVIS_SCHEDULE_MODEL", "") or HELPER_MODEL
SCHEDULE_EFFORT = os.environ.get("JARVIS_SCHEDULE_EFFORT", "") or "medium"
# A finished run's yes or no, read from its summary as JSON by one short call to VERDICT_MODEL: whether a watch's check
# found what it waits for, an only_changes run found nothing new, or a fix says its command should run again
VERDICT_MODEL = os.environ.get("JARVIS_VERDICT_MODEL", "") or FAST_MODEL
# A command task (a shell job, or a scheduled command's run: jarvis.tasks.command_jobs) that succeeds is summed up from
# the end of its output by one short call to COMMAND_SUMMARY_MODEL; one that fails is read by one short call to
# COMMAND_TRIAGE_MODEL, which says why and decides whether a task fixes it on its own, waits for the user's yes, or not
COMMAND_SUMMARY_MODEL = os.environ.get("JARVIS_COMMAND_SUMMARY_MODEL", "") or FAST_MODEL
COMMAND_TRIAGE_MODEL = os.environ.get("JARVIS_COMMAND_TRIAGE_MODEL", "") or HELPER_MODEL
# Claude usage (jarvis.brain.usage): what the CLI last reported of the plan's limits, shared by the voice and the runner.
# Scheduled runs that need a model wait for the five-hour window's reset once this fraction of it is used (1 never),
# and for any window's reset once it's used up.
USAGE_FILE = DATA_DIR / "usage.json"
USAGE_HOLD = _number(os.environ.get("JARVIS_USAGE_HOLD", ""), 0.9, 0.5, 1.0)
# Sign-ins (jarvis.integrations.signins): the heartbeat warns this many days before a GitHub or GitLab token expires
SIGNIN_WARN_DAYS = int(_number(os.environ.get("JARVIS_SIGNIN_WARN_DAYS", ""), 7, 1, 60))

# Learning review (jarvis.tasks.learning): once a conversation is over (hung up, or quiet this many minutes), one short
# model call reads it and suggests lasting facts for long-term memory, as a task's suggested edits
LEARNING_ENABLED = _flag("JARVIS_LEARNING", True)
LEARNING_MODEL = os.environ.get("JARVIS_LEARNING_MODEL", "") or FAST_MODEL
LEARNING_QUIET_MINUTES = max(1, int(os.environ.get("JARVIS_LEARNING_QUIET_MINUTES", "") or "10"))

# Skills (jarvis.skills): how-tos in Claude Code's own format, a folder with a SKILL.md each, kept in the profile's skills
# folder and offered to the voice brain and to tasks while they're switched on. Scripts in one installed from elsewhere
# are read by a short call to SKILL_REVIEW_MODEL, which says what they do, before the user can switch it on.
SKILL_REVIEW_MODEL = os.environ.get("JARVIS_SKILL_REVIEW_MODEL", "") or HELPER_MODEL

# Web Push (jarvis.notify.push): heartbeat alerts and task updates on the phone's home-screen app, even when it's closed
PUSH_ENABLED = _flag("JARVIS_PUSH", True)  # sends nothing until a device subscribes
# The VAPID "sub" claim push services may use to reach whoever runs the server: mailto: or https:. Unset, the
# default Google account's address (jarvis.notify.push.contact)
PUSH_CONTACT = os.environ.get("JARVIS_PUSH_CONTACT", "").strip()
VAPID_KEY_FILE = SECRETS_DIR / "vapid_private.pem"  # generated on first use
# APNs (jarvis.notify.apns): the same pushes in the JARVIS iPhone app, whose web view has no Web Push. The auth key comes
# from developer.apple.com → Certificates, IDs & Profiles → Keys → + (Apple Push Notifications service).
# Its 10-character key id is in the vault (jarvis.core.credentials), entered in the control panel.
APNS_KEY_FILE = SECRETS_DIR / "apns_key.p8"
APNS_TEAM_ID = os.environ.get("JARVIS_APNS_TEAM_ID", "").strip()  # the developer team's id (Membership details)

# Voice, all on-device: Whisper via MLX in, Kokoro out. Whisper large-v3-turbo quantized to 4 bits: about 0.5 GB
# in memory rather than 1.6 GB at 16 bits, which matters on an 8 GB Mac.
STT_MODEL = os.environ.get("JARVIS_STT", "") or "mlx-community/whisper-large-v3-turbo-q4"
# The speech vocabulary (jarvis.state.vocabulary) as Whisper's prompt, read at the start of each conversation; off
# leaves Whisper unprompted, and only the known mishearings are put right
STT_PROMPT = _flag("JARVIS_STT_PROMPT", True)
# Spare job processes kept warm, so a call connects at once. Whisper and Kokoro live in one shared process
# (jarvis.voice.speech), so a spare holds only the VAD, noise filter and brain code (~300 MB, not the ~2.5 GB it took
# with its own models). 0 saves that on an 8 GB Mac, and a call waits a few seconds for its process to start.
VOICE_IDLE_PROCESSES = max(0, int(os.environ.get("JARVIS_VOICE_IDLE_PROCESSES", "") or "1"))
# Memory the shared speech process keeps after it's done with it, which made it grow to 2.3 GB on an 8 GB Mac:
# MLX keeps Whisper's freed GPU buffers for reuse, about 0.7 GB after one clip. This caps that cache, in MB: 256 cost
# nothing measurable; 64 saves 0.2 GB more for about 25 ms a clip; 0 frees them after every clip; -1 = no cap.
MLX_CACHE_MB = int(os.environ.get("JARVIS_MLX_CACHE_MB", "") or "256")
# Kokoro's ONNX Runtime memory arena grows to fit the longest sentence yet and never shrinks (about 1 GB after a
# 20-second one). Off by default: synthesis was as fast without it.
TTS_MEMORY_ARENA = _flag("JARVIS_TTS_MEMORY_ARENA", False)
# macOS's malloc keeps large freed blocks in a cache, still counted against the process, so without the arena Kokoro's
# freed buffers stayed anyway. Off by default for the voice agent's child processes (the speech process, job processes
# and what they start): their memory goes back to the system when freed.
VOICE_MALLOC_LARGE_CACHE = _flag("JARVIS_VOICE_MALLOC_LARGE_CACHE", False)
# How often the voice agent logs its processes' memory (jarvis.voice.ram), in minutes; 0 = never
VOICE_MEMORY_LOG_MINUTES = max(0.0, float(os.environ.get("JARVIS_VOICE_MEMORY_LOG_MINUTES", "") or "10"))
# End of turn: seconds of silence before a finished-sounding turn is over, and the most it waits when the turn
# detector thinks the sentence is still going. Counted from the last speech, so Whisper's own time is part of it.
# Where each user's own delays start; they then follow that user's pace (jarvis.voice.turn).
TURN_MIN_DELAY = float(os.environ.get("JARVIS_TURN_MIN_DELAY", "") or "0.9")
TURN_MAX_DELAY = float(os.environ.get("JARVIS_TURN_MAX_DELAY", "") or "4.0")
# When the turn detector is unsure whether a pause ends the turn, a fast model reads what's been said and decides,
# giving up after TURN_JUDGE_SECONDS (the detector's verdict then stands); JARVIS_TURN_JUDGE=off leaves it to the
# detector alone
TURN_JUDGE = _flag("JARVIS_TURN_JUDGE", True)
TURN_JUDGE_SECONDS = _number(os.environ.get("JARVIS_TURN_JUDGE_SECONDS", ""), 2.0, 0.3, 6.0)
# A turn Whisper heard with an average log-probability under this is marked as unsure for the brain, which then checks
# back with its best guess when the words don't quite make sense
WHISPER_UNSURE_LOGPROB = _number(os.environ.get("JARVIS_WHISPER_UNSURE_LOGPROB", ""), -1.0, -10.0, 0.0)
# The session's Silero VAD: seconds of unbroken speech before an utterance starts (LiveKit's default is 0.05, which a
# click or breath of three 32 ms windows passes, and Whisper then hears a phantom "Thank you." in its padding), and
# the speech probability that counts. Barge-in still needs 0.5 s of speech, counted from the first window either way.
VAD_MIN_SPEECH = float(os.environ.get("JARVIS_VAD_MIN_SPEECH", "") or "0.2")
# Seconds of silence that end an utterance, so Whisper can start on it (LiveKit's default). Whisper's text is what ends
# a turn, so this is the floor under every reply's wait; lower ends turns sooner but cuts more mid-sentence fragments.
VAD_MIN_SILENCE = _number(os.environ.get("JARVIS_VAD_MIN_SILENCE", ""), 0.55, 0.3, 2.0)
VAD_THRESHOLD = float(os.environ.get("JARVIS_VAD_THRESHOLD", "") or "0.5")
VOICE_TURNS_LOG = ROOT / "logs" / "voice-turns.jsonl"  # each voice turn's timing, stage by stage (jarvis.voice.timing)


def tts_voice() -> str:
    """The Kokoro voice: b* British, a* American; then m or f."""
    return preference("voice") or DEFAULTS["voice"]


def tts_speed() -> float:
    return _number(preference("voice_speed"), float(DEFAULTS["voice_speed"]), *SPEED_RANGE)


TTS_VOICE, TTS_SPEED = tts_voice(), tts_speed()
# The Kokoro model file, a name in models/ or a full path. The full 32-bit one (326 MB) by default: on an M2 Air the
# 8-bit kokoro-v1.0.int8.onnx (92 MB) saves about 230 MB but renders 30-60% slower, slower than speech under load,
# which leaves a pause before each sentence. When the chosen file is missing, the first of the fallbacks that exists.
TTS_MODEL = ROOT / "models" / (os.environ.get("JARVIS_TTS_MODEL", "") or "kokoro-v1.0.onnx")
TTS_MODEL_FALLBACKS = (ROOT / "models/kokoro-v1.0.onnx", ROOT / "models/kokoro-v1.0.int8.onnx")
# Kokoro's ONNX Runtime threads. Empty: one fewer than the performance cores, 2 to 4 (3 on an M2), which rendered about
# as fast as all 8 cores under load, and leaves a core for the call itself; 0 = ONNX Runtime's own default.
TTS_THREADS = os.environ.get("JARVIS_TTS_THREADS", "").strip()
# Audio files (jarvis.voice.audio_file): text read into a file in Kokoro's voice, rendered as a job of its own, apart
# from the live voice. Its own Kokoro, the full 32-bit model by default (faster than the 8-bit one on Apple silicon;
# JARVIS_TTS_MODEL's file when it's missing), in a lower-priority process on AUDIO_THREADS of the CPU's cores (unset, a
# quarter of them, at least 2: on a busy 8 GB M2, 2 made audio faster than 4 or all 8), so the live voice keeps the
# rest. Files go to AUDIO_DIR, as m4a unless asked otherwise.
AUDIO_DIR = Path(os.environ.get("JARVIS_AUDIO_DIR", "") or "~/Downloads").expanduser()
AUDIO_FORMATS = ("m4a", "mp3", "wav")
AUDIO_FORMAT = (os.environ.get("JARVIS_AUDIO_FORMAT", "").strip().lower() or "m4a").removeprefix(".")
if AUDIO_FORMAT not in AUDIO_FORMATS:
    AUDIO_FORMAT = "m4a"
AUDIO_MODEL = ROOT / "models" / (os.environ.get("JARVIS_AUDIO_MODEL", "") or "kokoro-v1.0.onnx")
AUDIO_THREADS = max(0, int(os.environ.get("JARVIS_AUDIO_THREADS", "") or "0"))  # 0: a quarter of the cores
# Noise suppression on the mic (jarvis.voice.denoise): DeepFilterNet 3 on the CPU. on = the VAD, barge-in and Whisper all
# hear the filtered mic; vad = only the VAD does, and Whisper hears the original; off = the original everywhere
DENOISE_MODES = ("on", "vad", "off")
_denoise = os.environ.get("JARVIS_DENOISE", "").strip().lower()
DENOISE = {"1": "on", "true": "on", "yes": "on", "0": "off", "false": "off", "no": "off"}.get(_denoise, _denoise)
if DENOISE not in DENOISE_MODES:
    DENOISE = "on"
# The most it takes off any sound, in dB, so speech the model mistakes for noise stays audible to Whisper; 0 = no limit
DENOISE_ATTEN_DB = max(0.0, float(os.environ.get("JARVIS_DENOISE_ATTEN_DB", "") or "18"))
DENOISE_MODEL = ROOT / "models/deepfilternet3-streaming.onnx"
# The speech gate (jarvis.voice.speech_gate): drops clips with no real speech in them, judged mostly on the audio; the
# words only count as Whisper's known phantoms, which must clear a higher bar.
# shadow = log what it would drop and drop nothing; enforce = drop them; off = no checks
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
# Faint: little speech, quiet, and a short clip, all three (the phantoms that get past the checks above)
SPEECH_GATE_FAINT_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_FAINT_SPEECH_SECONDS", "") or "0.25")
SPEECH_GATE_FAINT_DBFS = float(os.environ.get("JARVIS_SPEECH_GATE_FAINT_DBFS", "") or "-28")
SPEECH_GATE_FAINT_MAX_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_FAINT_MAX_SECONDS", "") or "1.4")
# Phantom phrase: a transcript that is nothing but one of Whisper's known phantoms ("Thank you.", "You", "Bye.") only
# counts as said when its loudest 100 ms reaches PHANTOM_DBFS, the VAD hears PHANTOM_SPEECH_SECONDS of speech, and
# Whisper is sure of it. Real "yes", "no", "okay" and "go ahead" are never on the list. Comma-separated, to replace it.
SPEECH_GATE_PHANTOM_DBFS = float(os.environ.get("JARVIS_SPEECH_GATE_PHANTOM_DBFS", "") or "-22")
SPEECH_GATE_PHANTOM_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_PHANTOM_SPEECH_SECONDS", "") or "0.6")
SPEECH_GATE_PHANTOMS = [p.strip() for p in os.environ.get("JARVIS_SPEECH_GATE_PHANTOMS", "").split(",") if p.strip()]
# Whisper unsure: its average log-probability for the text is under MIN_LOGPROB (Whisper's own retry threshold: under it,
# every retry failed too and the text is a guess) and the VAD heard under UNSURE_SPEECH_SECONDS of speech
SPEECH_GATE_MIN_LOGPROB = float(os.environ.get("JARVIS_SPEECH_GATE_MIN_LOGPROB", "") or "-1.0")
SPEECH_GATE_UNSURE_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_UNSURE_SPEECH_SECONDS", "") or "0.5")
# Repetitive: the text's gzip compression ratio is over this (Whisper's own threshold): a phrase looping, which only
# a hallucination does
SPEECH_GATE_MAX_COMPRESSION = float(os.environ.get("JARVIS_SPEECH_GATE_MAX_COMPRESSION", "") or "2.4")
SPEECH_GATE_LOG = ROOT / "logs" / "speech-gate.jsonl"  # every clip it measured, kept or not
# Each clip it judged, as the WAV Whisper heard, to listen to what it made of it: on by default while in shadow mode


def speech_gate_save_audio(mode: str) -> bool:
    return _flag("JARVIS_SPEECH_GATE_SAVE_AUDIO", mode == "shadow")


SPEECH_GATE_SAVE_AUDIO = speech_gate_save_audio(SPEECH_GATE)
# Short clips it lets through (under SHORT_SECONDS, or under SHORT_SPEECH_SECONDS of speech) are saved even with
# SAVE_AUDIO off, so a phantom that slips past can be heard; the clips it drops aren't
SPEECH_GATE_SAVE_SHORT = _flag("JARVIS_SPEECH_GATE_SAVE_SHORT", True)
SPEECH_GATE_SHORT_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_SHORT_SECONDS", "") or "2.0")
SPEECH_GATE_SHORT_SPEECH_SECONDS = float(os.environ.get("JARVIS_SPEECH_GATE_SHORT_SPEECH_SECONDS", "") or "0.5")
SPEECH_GATE_AUDIO_DIR = ROOT / "logs" / "speech-gate-audio"
SPEECH_GATE_AUDIO_KEEP = max(1, int(os.environ.get("JARVIS_SPEECH_GATE_AUDIO_KEEP", "") or "300"))  # the newest files

# Speaker verification (jarvis.voice.speaker): once the user has enrolled a voiceprint (jarvis.state.voiceprints), each
# utterance is compared with it, and speech that isn't theirs (a TV, someone else in the room) never reaches the brain.
# on = drop it; shadow = log what it would drop and drop nothing; off = no checks. With no voiceprint, nothing changes.
SPEAKER_MODES = ("on", "shadow", "off")
SPEAKER_MODEL = ROOT / "models/wespeaker-voxceleb-resnet34-LM.onnx"  # make models; a voiceprint names the model it's from
SPEAKER_MODEL_ID = "wespeaker-voxceleb-resnet34-LM"


def speaker_check() -> str:
    mode = preference("speaker_check").lower()
    return mode if mode in SPEAKER_MODES else DEFAULTS["speaker_check"]


def speaker_threshold() -> float:
    """The cosine similarity to the voiceprint an utterance with SPEAKER_LONG_SECONDS of speech or more needs."""
    return _number(preference("speaker_threshold"), float(DEFAULTS["speaker_threshold"]), *SPEAKER_THRESHOLD_RANGE)


def _env_number(name: str, default: float, low: float = 0.0, high: float = 1e6) -> float:
    return _number(os.environ.get(name, "").strip() or str(default), default, low, high)


# Shorter utterances say less about who spoke, so they need less: SHORT_MARGIN under the threshold at SHORT_SECONDS of
# speech or less, sliding up to the full threshold at LONG_SECONDS. While the user is in a conversation (they were the
# last one heard, within RECENT_SECONDS, or JARVIS finished speaking under REPLY_SECONDS ago) a short one needs RELAX
# less again, so a "yes" isn't lost; one under MIN_SECONDS of speech is then kept whatever it scores, as too short to tell.
SPEAKER_SHORT_MARGIN = _env_number("JARVIS_SPEAKER_SHORT_MARGIN", 0.1, 0.0, 1.0)
SPEAKER_RELAX = _env_number("JARVIS_SPEAKER_RELAX", 0.1, 0.0, 1.0)
SPEAKER_SHORT_SECONDS = _env_number("JARVIS_SPEAKER_SHORT_SECONDS", 1.0)
SPEAKER_LONG_SECONDS = _env_number("JARVIS_SPEAKER_LONG_SECONDS", 3.0)
SPEAKER_MIN_SECONDS = _env_number("JARVIS_SPEAKER_MIN_SECONDS", 0.4)
SPEAKER_RECENT_SECONDS = _env_number("JARVIS_SPEAKER_RECENT_SECONDS", 30.0)
SPEAKER_REPLY_SECONDS = _env_number("JARVIS_SPEAKER_REPLY_SECONDS", 10.0)
# Within a conversation, the user's own clearly verified utterances (LONG_SECONDS of speech, ADAPT_MARGIN over the
# threshold) also count as a reference, so the voiceprint follows this call's mic and room; off = the voiceprint only
SPEAKER_ADAPT = _flag("JARVIS_SPEAKER_ADAPT", True)
SPEAKER_ADAPT_MARGIN = _env_number("JARVIS_SPEAKER_ADAPT_MARGIN", 0.1, 0.0, 1.0)
# Enrolling: the speech it takes (silences don't count), the least it accepts, and how long a spoken enrollment waits
SPEAKER_ENROLL_SECONDS = _env_number("JARVIS_SPEAKER_ENROLL_SECONDS", 25.0, 5.0, 300.0)
SPEAKER_ENROLL_MIN_SECONDS = _env_number("JARVIS_SPEAKER_ENROLL_MIN_SECONDS", 12.0, 3.0, 300.0)
SPEAKER_ENROLL_TIMEOUT = _env_number("JARVIS_SPEAKER_ENROLL_TIMEOUT", 120.0, 10.0, 3600.0)
SPEAKER_LOG = ROOT / "logs" / "speaker-check.jsonl"  # every utterance it judged: score, what it needed, verdict
# Barge-in (jarvis.voice.barge_in): while JARVIS replies, speech interrupts it only once the check has heard it's the
# user's: judged from MIN_SECONDS of speech (LiveKit's own barge-in waits for 0.5 s of it anyway), then again every STEP
# of speech more, on the latest MAX_SECONDS of it, until it matches or ends. off = any voice interrupts, as before
SPEAKER_BARGE_IN = _flag("JARVIS_SPEAKER_BARGE_IN", True)
SPEAKER_BARGE_IN_MIN_SECONDS = _env_number("JARVIS_SPEAKER_BARGE_IN_MIN_SECONDS", 0.5, 0.2, 5.0)
SPEAKER_BARGE_IN_MAX_SECONDS = max(SPEAKER_BARGE_IN_MIN_SECONDS,
                                   _env_number("JARVIS_SPEAKER_BARGE_IN_MAX_SECONDS", 1.0, 0.2, 6.0))
SPEAKER_BARGE_IN_STEP = _env_number("JARVIS_SPEAKER_BARGE_IN_STEP", 0.25, 0.05, 2.0)

# The web browser (jarvis.integrations.browser): JARVIS's own Chrome, driven by Playwright, with a profile of its own
# that keeps the logins the user makes in it (in their profile's data folder). Preferences, read afresh: whether the voice brain gets the browser tools
# (from the next conversation), and whether its window is shown (headed) or not (headless), from its next launch.
BROWSER_WINDOWS = ("headed", "headless")


def browser_on() -> bool:
    return _switch(preference("browser"), True)


def browser_headless() -> bool:
    return preference("browser_window").lower() == "headless"


# The check for hidden instructions in what tools bring in (jarvis.safety.injection): whether it's on, and its model,
# which the Settings panel picks from INJECTION_MODELS (.env may name any)
INJECTION_MODELS = (FAST_MODEL, HELPER_MODEL)


def injection_check_on() -> bool:
    return _switch(preference("injection_check"), True)


def injection_model() -> str:
    return preference("injection_model") or DEFAULTS["injection_model"]


# The launchd services (deploy/, installed by jarvis.setup_local) are labelled SERVICE_PREFIX.<name>: com.jarvis.agent
# and so on. Labels only have to be unique among the user's own services, so the prefix only needs changing for a
# second JARVIS under the same Mac account.
SERVICE_NAMES = ("livekit", "agent", "web", "tasks")
SERVICE_PREFIX = os.environ.get("JARVIS_SERVICE_PREFIX", "").strip().strip(".") or "com.jarvis"


def service_label(name: str) -> str:
    """The launchd label of one of JARVIS's services, by its name in SERVICE_NAMES: "agent" is com.jarvis.agent."""
    return f"{SERVICE_PREFIX}.{name}"


# Local LiveKit server (deploy/livekit.yaml) and the web app for the phone
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "").strip() or "ws://127.0.0.1:7880"
WEB_PORT = int(os.environ.get("JARVIS_WEB_PORT", "") or "8787")
# Who may connect: each person's name and passcode, hashed (jarvis.core.logins). The LiveKit keys are in the vault
# (jarvis.core.credentials).
LOGINS_FILE = SECRETS_DIR / "logins.json"

# Google: one OAuth client, one token per account label. The accounts themselves are in the database
# (jarvis.integrations.accounts), each with its label, email, note and whether it has Google Chat, and the default.
# The client is in the vault (jarvis.core.credentials), entered in the control panel. Until it is, a JSON file from
# Google Cloud stands in: this, or the client_secret*.json in secrets/.
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip() or next(
    (str(f) for f in sorted(SECRETS_DIR.glob("client_secret*.json"))), "")

# MCP servers (GitHub, GitLab and any others) are in the database (jarvis.integrations.mcp_servers), their tokens encrypted there
# (jarvis.core.vault).
