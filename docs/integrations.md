<div align="center">

<img src="assets/logo.svg" width="72" alt="">

# 🔌 Integrations

**Everything JARVIS connects to beyond the conversation, how each one is kept safe, and how to add a tool of your own.**

[🏠 Home](../README.md) · [🚀 Setup](setup.md) · [⚙️ Configuration](configuration.md) · [🗣️ Voice](voice.md) · [🛠️ Tasks](tasks.md) · **🔌 Integrations** · [📱 Mobile](mobile.md) · [👩‍💻 Development](development.md) · [🏗️ Architecture](architecture.md)

</div>

---

Reading runs at once. Anything that **changes** something waits for your spoken **yes**. Pick an integration to jump to it:

<table>
<tr>
<td width="33%" valign="top">

**[📬 Google](#-google-accounts)**<br>
Reads mail, calendar, Sheets, Docs, Slides, Drive. **After your yes:** drafts and sends mail, events, Sheets and Docs edits.

</td>
<td width="33%" valign="top">

**[💬 Google Chat](#-google-chat)**<br>
Reads DMs and spaces, and searches. **After your yes:** replies and new DMs. Marking read needs no yes.

</td>
<td width="33%" valign="top">

**[🔌 Connections](#-connections-mcp-servers)**<br>
MCP servers from the official registry. Each server's reading tools run at once; its **changing tools wait for your yes**.

</td>
</tr>
<tr>
<td valign="top">

**[🩺 Sign-in checks](#-sign-in-checks)**<br>
Each connection can name a reading tool JARVIS calls hourly, so you **hear when a key or sign-in stops working**.

</td>
<td valign="top">

**[🔒 Secrets vault](#-secrets-vault)**<br>
Keys and sign-ins, **AES-256-GCM** encrypted. Only the server launcher decrypts.

</td>
<td valign="top">

**[🌐 Web browser](#-web-browser)**<br>
Its own Chrome for pages behind your logins. **Read-only:** changes nothing.

</td>
</tr>
<tr>
<td valign="top">

**[📷 Pictures](#-pictures)**<br>
Photos you send from the app, and the screen when you ask.

</td>
<td valign="top">

**[🎧 Recordings](#-recordings)**<br>
Audio and video files transcribed on JARVIS's computer, answered from the transcript.

</td>
<td valign="top">

**[🔊 Audio files](#-audio-files)**<br>
Text or documents read aloud into a file in your download folder.

</td>
</tr>
<tr>
<td valign="top">

**[💻 Desktop tools](#-desktop-tools)**<br>
Notifications, opening things, volume and the clipboard, on macOS or a Linux desktop.

</td>
<td valign="top">

**[🧩 Skills](#-skills)**<br>
Claude Code-format how-tos, installed, written or switched on after your yes.

</td>
<td valign="top">

**[🔧 Adding a tool](#-adding-a-tool)**<br>
Your own tool: one decorated function under `jarvis/tools/`, plus a test.

</td>
</tr>
</table>

## 🔐 Ground rules

The same guards apply to every integration, and they're **enforced in code**:

| | Rule | What it means |
|:-:|---|---|
| 🗣️ | **Sends are gated** | Mail, Chat messages and changing MCP calls are read aloud, then run after a clear yes |
| 🔒 | **Secrets live in the vault** | Keys are pasted, never spoken; encrypted, never shown again |
| 🌐 | **The browser only reads** | Nothing clicked, typed or submitted; local and private addresses refused |
| 🧾 | **Content is data** | What mail, pages and connections return is never taken as instructions |
| 🚫 | **Unsorted tools are refused** | A connection's tool that isn't sorted into reading or changing never runs |

## 📬 Google accounts

Accounts live in the `accounts` table of `data/tasks.db`. Each one has:

| Field | Meaning |
|---|---|
| **label** | What you call it; also names its sign-in file `secrets/google-<label>.json` |
| **email** | The account's address |
| **note** | Optional; JARVIS is told it |
| **chat** | Whether it has Google Chat |
| **default** | Exactly one account is the default |

| Request | Account used |
|---|---|
| Searches and overviews ("any new email?", "what's on today?") | **Every account**, unless you name one |
| Anything made or changed without naming one | **The default** |
| Naming one | By label or email |

A failing account is skipped during a search, and named.

**From the control panel** — **JARVIS → Google accounts**: add an account, sign it in (or in again), set its note and the default, or remove it. Sign-in opens Google in a new tab and comes back by itself, except on a phone with a Desktop app client, where you paste the address Google's last page ends on (see [setup](setup.md#-5-google)).

**From the terminal** — on JARVIS's computer, for what the panel doesn't do (rename, email, Chat on an existing account). Signing in is only in the panel:

```sh
make accounts                                  # list
make accounts ARGS='add home --note "my own"'  # also --chat, --default; then sign it in from the panel
make accounts ARGS="rename home family"        # moves its sign-in and Chat state
make accounts ARGS="remove family"             # and deletes its sign-in
make accounts ARGS="default work"
make accounts ARGS='set work --email me@example.com --note "for work" --chat'
```

> [!NOTE]
> **No restart:** each conversation, background task and watch check starts by rereading the accounts, so a change applies from the next one. With no accounts, JARVIS runs without the Google tools.

### Permissions

| Permission | For |
|---|---|
| Calendar | Your calendar |
| Gmail read · compose · modify | Reading; drafts and sending after your yes; read/unread, archive, star, labels |
| Google Sheets, Google Docs | See, edit and create |
| Drive metadata (read-only) | File names and details |
| Drive files JARVIS makes (`drive.file`) | Images put into Docs |
| Drive read-only (`drive.readonly`) | Searching inside files, reading Slides. **Never changes, shares or deletes** |
| Chat (Chat accounts only) | Reading, replying, marking read, starting DMs |

> [!TIP]
> When an account was signed in before a permission it now needs, JARVIS says so. Press **Sign in again** beside it in the control panel, allow every permission listed, and you're done: **no restart**. If the consent screen doesn't show the new permissions, remove JARVIS's access at https://myaccount.google.com/permissions and try again.

## 💬 Google Chat

On accounts marked `--chat` (Workspace only: personal Gmail has no Chat API).

| Say | What happens |
|---|---|
| "anything on Chat?" | Unread: DMs first, then spaces where you were @mentioned |
| "summarise my DM with Sam" · "what's new in Platform Dev?" | Reads one conversation |
| "find the Chat message about the player fix" | Searches |
| "mark the alert spaces as read" | Done at once, no yes needed (only your own read state) |
| "reply to Sam that I'll join at four" | Reads the exact draft and recipient aloud, sends after your yes |

- **Sending is enforced in code:** a clear yes within five minutes; for a space, JARVIS says how many people are in it. Background tasks and watches can read Chat but **never send**.
- **New DMs** to someone you've never messaged are drafted as "To Sam Lee, in a new direct message: …" and created only on send.
- **People** are found loosely by name (speech recognition) from the Workspace directory and your chats; if several match, JARVIS asks which.

### One-time set-up

In the Google Cloud project that owns JARVIS's OAuth client:

**1. Enable the APIs** — the **Google Chat API** and the **People API**.

**2. Configure the Chat app** — on the Chat API's Configuration page: a name, an avatar URL and a description. Leave "Build this Chat app as a Workspace add-on" **unticked** (it can't be undone); interactive features can stay off.

**3. Sign the account in for Chat** — add it in the control panel with **Google Chat** switched on (or mark an existing one with `make accounts ARGS="set work --chat"`), then sign in from the panel and allow the Chat permissions.

The sign-in then checks Chat and says what to fix. `make chat-check` reruns that check.

> [!WARNING]
> **If your Workspace blocks Chat** ("Access blocked", `admin_policy_enforced`), close the tab: the control panel offers to retry without reading messages, then without Chat. With Chat blocked, "anything on Chat?" reads Chat's notification emails through Gmail instead, so turn those on in Chat's settings.

## 🔌 Connections (MCP servers)

JARVIS finds and connects new tools for you as **MCP servers**. Say "connect Notion", "can you get access to Linear?", or ask for something it has no tool for.

| | Step | What happens |
|:-:|---|---|
| 🔍 | **Find** | The [official MCP registry](https://registry.modelcontextprotocol.io) first; failing that, the web, marked **unverified** |
| 📦 | **Connect** | Says what it would install, from where, how it runs and what it needs. **Nothing installs until you say yes** |
| 🔑 | **Keys** | Pasted in **Settings → Connections → Enter key**. A key said aloud is refused |
| 🌐 | **Server** | A self-hosted service (your company's GitLab, GitHub Enterprise) is set in **Settings → Connections → Change server**, as an `https://` address |
| 🔐 | **Sign-in** | OAuth services sign in through JARVIS's own browser on its computer |
| 🗂️ | **Sort its tools** | **Reading** tools run at once; **changing** tools each wait for your yes |
| 🔁 | **Later** | "What tools are connected?", "switch Notion off", "is there a newer Notion?", "remove Notion" |

- **Runs** hosted, or locally with `npx`/`uvx`/`docker` at a **pinned version**.
- **Keys and sign-ins** are kept encrypted; a key is never shown again.
- **Sorting** follows the server's own `readOnlyHint` / `destructiveHint`; ask to move any tool or switch it off.
- **Removing** a server says what goes with it first.
- **Changing its server** is the one setting the control panel changes (`GITLAB_API_URL`, `GITHUB_HOST`, or a setting of its own ending in `_URL` or `_HOST`). Moving to **another host forgets its keys**, as they were for the old server, so a control-panel login can't send them elsewhere; paste the new server's key after.

A changing call is read out from the tool's own title and arguments ("On Notion, create a page, parent Projects, title 'Q4 plan'. Okay to go ahead?") and runs once after a clear yes. **What a connection returns is data, never instructions**: JARVIS connects, updates or removes only on your own request.

### Tool access

| Access | Behaviour |
|---|---|
| `read`, `own` | Run at once (`own` changes only your own state, like marking a notification done) |
| `write` | Waits for your yes, with a phrase saying what it does |
| `blocked` | Never offered |
| *not listed* | Refused |

| Who | Gets |
|---|---|
| 🎙️ The voice brain | Every enabled, sorted server, with its keys or sign-in |
| 🔬 Research tasks | Servers marked for research (all made by voice are), reading tools only |
| 💻 Code tasks | None |

- **The relay.** Each server runs behind a small relay (`jarvis/integrations/mcp_relay.py`) that runs reading calls **side by side** and follows MCP's long-running tasks.
- **Slow calls** still running after `JARVIS_CONNECTION_WAIT` (20 s) move into the **background**, so the conversation carries on.
- **Tool search.** Tools load on demand (`JARVIS_TOOL_SEARCH`), so more connections don't slow every reply.
- **No restart.** Changes apply from the next conversation.

### By hand

<details>
<summary><b>⌨️ The <code>make servers</code> commands</b></summary>

<br>

```sh
make servers                                              # list, with what's missing
uv run python -m jarvis.integrations.mcp_registry notion  # what the registry has
make servers ARGS="add acme --label Acme --command npx --arg=-y --arg @acme/mcp@1.2.0 --secret-env ACME_TOKEN=acme.token"
make servers ARGS="add docs --label Docs --url https://mcp.docs.example/mcp --secret-env KEY=docs.key --header 'Authorization=Bearer {KEY}'"
make servers ARGS="test acme"                             # start it, list tools and their markings
make servers ARGS="tools acme --sort --block wipe"        # sort by markings (or --read ... --write 'post=post to {channel}')
make servers ARGS="set-secret acme"                       # typed in hidden, or stdin; never on the command line
make servers ARGS="disable acme"                          # or enable, remove, set NAME --env K=V, clear-secret
make servers ARGS="set gitlab --env GITLAB_API_URL=https://git.example.com/api/v4"  # its server, keeping its key
```

</details>

Package runners (`npx`, `uvx`) must name a **pinned version**. Settings and headers that look like credentials are **refused** outside `--secret-env` and the vault.

## 🩺 Sign-in checks

Keys expire and sign-ins get revoked. So a tool doesn't fail you mid-conversation, each connection can name a **sign-in check**: one of its reading tools that takes no arguments and only confirms who is signed in. JARVIS picks it while sorting the tools (it's in the summary you say yes to), and you can change it with "make *that* its sign-in check".

- **Hourly.** The notices service calls each switched-on connection's check about once an hour.
- **Said once.** A refusal is said once, as a notification, a phone push and a spoken heads-up, and again only after the check has passed in between.
- **Silent when unreachable.** A server that doesn't start or answer at all isn't reported as a sign-in problem.
- **By hand:** `make servers ARGS="tools acme --check whoami"`, or `--check ''` for none. Run it now with `make servers ARGS="check acme"` (or `--tool whoami` for another reading tool): it says only whether it passed, never what the tool answered.

## 🔒 Secrets vault

| | Part | How it works |
|:-:|---|---|
| 🗄️ | **Storage** | `secrets` table of `data/tasks.db`, **AES-256-GCM** under `JARVIS_MASTER_KEY` (32 random bytes in `.env`) |
| 🔓 | **Who decrypts** | Only `python -m jarvis.integrations.mcp_launch`, starting each server with **its own** secrets |
| 🙈 | **Who doesn't** | The CLI, shells and tasks never see the key or a token |
| 🚫 | **No peeking** | Reading other processes' environments (`ps eww`, `/proc/<pid>/environ`) is refused, even with a yes |

```sh
make vault                               # the key's id and secrets' names, never a value
make vault ARGS=init                     # make the key (setup does this)
make vault ARGS=rotate                   # new key, everything re-encrypted; old key kept after it
make vault ARGS="reencrypt --drop-old"   # then drop the old key
make vault ARGS="reset --yes"            # lost key: forget every secret and start again
```

> [!CAUTION]
> The key and `data/tasks.db` together open every secret. **Keep them apart**: whoever has `.env` shouldn't also have the database.

## 🌐 Web browser

JARVIS has its own Chrome, driven by [Playwright](https://playwright.dev/python/), for pages that need your logins or JavaScript ("what's on my Jira board?"). It uses **a profile of its own** in `data/browser/profile`: you log in there once, yourself, and two-factor codes stay with you. Sandboxed commands and tasks can't read its cookies.

| | Rule | Detail |
|:-:|---|---|
| 👀 | **Read-only** | Nothing is clicked, typed or submitted |
| 🎙️ | **Voice brain only** | Background tasks don't get it |
| 🧾 | **Untrusted content** | Page text comes back fenced as web content, never instructions |
| 🌍 | **Only the web** | `http`/`https` only; local, private and Tailscale addresses refused |
| 🤖 | **Captchas** | Reported, never worked around |
| 🪟 | **One window per conversation** | Opens on first use, closes when the conversation ends |

- **Its tools:** `browser_open`, `browser_read`, `browser_links`, `browser_screenshot`, `browser_back`, `browser_close`.
- **Refused addresses:** localhost, private, link-local, Tailscale and `.local`, also for redirects, frames and scripts, through JARVIS's own checking proxy.
- **Switches:** `JARVIS_BROWSER` and `JARVIS_BROWSER_WINDOW` (or Settings → Behaviour) switch it off or hide the window.

**Set-up** — install Chromium (used when Google Chrome isn't installed), then ask JARVIS to open a site and log in in the window:

```sh
uv run playwright install chromium
```

## 💻 Desktop tools

Small tools for the computer JARVIS runs on. **None waits for a yes**; the changing ones are listed in the bell. Clipboard text is **data**, read only when you ask ("read what I copied").

| | Does | macOS | Linux |
|:-:|---|---|---|
| 🔔 | **Notify** | `mac_notify` | `desktop_notify` |
| 📂 | **Open** | `mac_open`: an app by name, or a URL | `desktop_open`: a link, file or folder, **not apps by name** |
| ⚡ | **Shortcuts** | `mac_run_shortcut`, `mac_list_shortcuts` | No equivalent |
| 🔊 | **Volume** | `mac_set_volume` | `desktop_set_volume` |
| 📋 | **Clipboard** | `mac_copy`, `mac_read_clipboard` | `desktop_copy`, `desktop_read_clipboard` |

- **macOS:** the tools come from AI Armory and run macOS's own programs.
- **Linux:** each tool is offered only while **its program is installed** and a **desktop session** is reachable (`DISPLAY` or `WAYLAND_DISPLAY`). Task notifications use the same program.
- **Services on Linux:** `make services-install` and `make services-start` hand your session's display and D-Bus address to the systemd user services, so run them from a terminal in your desktop session.
- **Your folders:** Downloads, Desktop and Documents (searched for a bare file name; audio files go to Downloads) follow the **XDG user folders** on Linux (`~/.config/user-dirs.dirs`), translated names included.

**Linux packages** (Debian/Ubuntu names) for these tools and the sections below:

| | For | Install |
|:-:|---|---|
| 🔔 | Notifications | `libnotify-bin` |
| 📂 | Opening links, files, folders | `xdg-utils` |
| 📋 | Clipboard | `wl-clipboard` (Wayland), or `xclip` / `xsel` (X11) |
| 🔊 | Volume | `wireplumber` (`wpctl`), or `pulseaudio-utils` (`pactl`) |
| 🖥️ | [Screenshots](#-pictures) | `grim` (Wayland, wlroots), `gnome-screenshot`, `spectacle` (KDE), or `scrot` / `imagemagick` (X11) |
| 🔊 | [Audio files](#-audio-files) as m4a or mp3 | `ffmpeg` (`lame` also makes mp3) |
| 📄 | Reading `.doc`, `.rtf`, `.odt` | `libreoffice-writer`, or `pandoc` (not `.doc`) |

- **Under WSL2** JARVIS can't see the Windows screen, so **screenshots are refused**. Notifications and the clipboard work only under **WSLg**, inside Linux's own windows, not Windows'.

## 📷 Pictures

| | What | How |
|:-:|---|---|
| 📸 | **Show JARVIS a photo** | The camera button in the message box, then ask ("what's this plant?"). Shrunk and sent with your next turn |
| 🖥️ | **Look at the screen** | "What's this error on my screen?" takes a silent screenshot of the computer's main display |
| 🗓️ | **Kept** | Photos for a week, screenshots for a day |

The screenshot is taken **only when your own words that turn ask for it**.

| | macOS | Linux |
|---|---|---|
| **Photos** (HEIC, TIFF, oversized) | Converted to JPEG with `sips` | Converted with **Pillow**; HEIC through `pillow-heif`, installed with JARVIS |
| **Screenshots** | `screencapture` | The first that works: `grim`, `gnome-screenshot`, `spectacle` (Wayland); `scrot`, ImageMagick's `import` (X11) |
| **Set-up** | Allow the voice agent under System Settings → Privacy & Security → **Screen Recording**, then `make services-restart` | Install a screenshot program ([packages](#-desktop-tools)). A black or blank shot means the desktop refused; WSL2 can't take one |

## 🎧 Recordings

"What did they decide in standup.m4a?", "summarise the voice note in that email". JARVIS transcribes an audio or video file, then answers from the transcript.

| | What | Detail |
|:-:|---|---|
| 🔎 | **Finds** | m4a, mp3, wav, mp4, mov... by name in Downloads, Desktop or Documents ([XDG folders](#-desktop-tools) on Linux), or attached to an email |
| 📦 | **Nothing to install** | Whisper in a process of its own: the live voice's model on Apple silicon (about 0.5 GB while it works), multilingual `small` on a CPU, `large-v3-turbo` on an NVIDIA GPU (`JARVIS_TRANSCRIBE_MODEL`) |
| 🐢 | **Long ones** | Carry on in the background after 45 seconds; ask again later |
| 🗓️ | **Kept** | In the transcripts folder, timestamped, for 30 days |

One recording at a time, up to **4 hours** each. The same recording isn't transcribed twice.

## 🔊 Audio files

"Make me an audio file of that summary", "read invoice-notes.pdf into an mp3", "turn this morning's briefing into audio and send it to my phone".

| | What | Detail |
|:-:|---|---|
| 📄 | **Reads** | Text JARVIS writes or you dictate, or a document on the computer (PDF, Word, text...) |
| 🗣️ | **Voice** | Kokoro, with your voice and speed |
| ⚙️ | **Runs** | As a [long job](tasks.md#long-jobs), so the live voice is never held up |
| 📁 | **Goes to** | Your download folder (`~/Downloads/Title.m4a`), never overwriting; mp3 or WAV on request |
| 📱 | **To the phone** | A push when ready opens a player and a download link |

- **Markdown** is read as plain speech.
- **Long jobs** run at lower priority on a few cores, one file at a time, up to ~12 hours of audio.
- **Settings:** `JARVIS_AUDIO_DIR` and `JARVIS_AUDIO_FORMAT`.
- **The phone link** is valid for a week, and for that file only.

| What it needs | macOS | Linux |
|---|---|---|
| **m4a** (default) | Nothing: built-in `afconvert` | `ffmpeg` |
| **mp3** | `ffmpeg` or `lame` (`brew install ffmpeg`) | `ffmpeg` or `lame` |
| **WAV** | Nothing | Nothing |
| **`.doc`, `.rtf`, `.odt`** to read | Nothing: built-in `textutil` | LibreOffice, or pandoc for `.rtf` and `.odt` |

Without the encoder, JARVIS says what to install. PDF, Word (`.docx`), Excel and text files read the same everywhere.

## 🧩 Skills

How-tos for workflows you repeat, in **Claude Code's own skill format** (a folder with a `SKILL.md`), kept in `data/skills/` and offered to the voice brain and tasks while switched on. Manage them in **Settings → Skills**.

| | Way in | How |
|:-:|---|---|
| ✍️ | **Write together** | "Let's make a skill for my standup notes": drafted, summarised aloud, saved after your yes |
| 📄 | **From a file** | "Save task 12's SKILL.md as my review skill": read in place from a task's folder or your Desktop, Documents or Downloads (never a secret), summarised aloud with where it came from, saved after your yes |
| 📥 | **Install** | "Install the skill from owner/repo": fetched over https from GitHub or a zip, staged, described, installed after your yes |
| 💡 | **Learned** | The [learning review](tasks.md#-learning-review) may suggest one for a workflow you keep asking for |

**The walls around a skill:**

- **Risky frontmatter** (`allowed-tools`, `hooks`, `model`...) is stripped.
- **Hooks and inline** `` !`command` `` never run.
- **Symlinks** make a skill unusable.
- **A skill with scripts** stays off until you've heard a **review** (a fixed-pattern scan plus a short Sonnet summary) and approved exactly those files. Changed since, it's skipped.

## 🔧 Adding a tool

**1. Write the function** — decorate it with `@tool(name, description, properties, required)` in a module under `jarvis/tools/`.

**2. Register a new module** — if the module is new, import it at the bottom of `jarvis/tools/__init__.py`.

**3. Test it** — add a test in `tests/`.

Tools may be sync (run in a thread) or async. Useful flags on `@tool`:

| Flag | Effect |
|---|---|
| `reads=True` | Only reads, so it may run alongside other calls in the same step |
| `changes=True` / `said="..."` | It changes something, so the control panel's bell lists it |
| `gated=True` | Runs only after your spoken yes |
| `enabled=fn` | Offered only while `fn()` is true, checked as each session starts |

A gated tool follows the draft-then-confirm pattern in `jarvis/safety/draft_gate.py`. Return text, something JSON-serialisable, or `WithPictures` for images.

---

<div align="center">

[← 🛠️ Tasks](tasks.md) &nbsp;·&nbsp; [🏠 Home](../README.md) &nbsp;·&nbsp; [📱 Mobile →](mobile.md)

</div>
