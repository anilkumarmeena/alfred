# Natural conversation: the plan

The goal: talking to JARVIS should feel like talking to an assistant who is actively discussing things with you, not
like operating a voice robot. It answers quick questions at once, waits while you're still thinking, starts talking
as soon as it has something to say, keeps the conversation going while slow work runs, and brings things up itself
when the moment is right.

JARVIS is used by many people, so everything here is general and per user. Nothing is keyed on particular words or
phrases: where a judgement is needed (is the user finished? is now a good moment?), a model makes it, from what was
said and how the conversation stands. The one exception stays as it is: the word lists that read a yes or no to a
dangerous command (`jarvis/safety/consent.py`), where a fixed, auditable rule is the point.

Five stages, each shippable on its own, in this order because each one's measurements guide the next.

| Stage | What changes | Status |
|---|---|---|
| 1. Measure, and a fitting acknowledgement | Per-turn timing logs; the lead-in phrase fits the job | **Done** (this branch) |
| 2. Don't cut the user off | End of turn judged on meaning and on the user's own pace; imperfect speech handled by checking back | **Done** |
| 3. Speak as it thinks | First audio about a second after a simple question | **Done**, partly: see below |
| 4. Slow work in the background | The conversation carries on while long work runs | **Done** |
| 5. Proactivity | JARVIS raises things itself, at moments the model judges right | **Done** |

## How a turn works today

From `jarvis/voice/agent.py`, `jarvis/brain/cli.py` and LiveKit Agents 1.8.3:

1. **Hearing.** Mic audio goes through DeepFilterNet (`denoise.py`) to Silero VAD. The VAD cuts an utterance once it
   hears silence; the whole clip goes to Whisper (batch, not streaming) in the shared speech process (`speech.py`). The
   speech gate and speaker check judge the clip (`speech_gate.py`, `speaker.py`); vocabulary fixes known mishearings.
2. **End of turn.** LiveKit's local turn detector (`v1-mini`, a small text model reading the transcript) gives a
   probability that the user has finished. Above its `unlikely_threshold` the turn ends after `min_delay` of silence
   (`JARVIS_TURN_MIN_DELAY`, 0.9 s); below it, after `max_delay` (`JARVIS_TURN_MAX_DELAY`, 4 s). Both are fixed for
   everyone. Preemptive generation is off, so the brain only sees finished turns.
3. **Thinking.** `JarvisAgent.llm_node` takes the user's text. A yes or no to an open approval question is read first
   (`consent.reply`; a model call only when a question is open and the word lists can't read the answer). Otherwise
   `CliBrain.respond` sends it to one long-lived `claude` CLI session (Opus, effort `low`) and streams the text deltas
   back. Tool calls (calendar, mail, web search, Bash, a `lookup` subagent on Haiku) happen inside that one turn.
4. **Speaking.** LiveKit's `StreamAdapter` cuts the stream into sentences, and long sentences at their pauses
   (`clauses.py`: at most 12 words a piece, none under 4, nothing before 20 characters), and Kokoro renders each piece
   in the shared speech process while the one before plays. So streaming to speech sentence by sentence is already in
   place; what's slow is getting the first sentence.
5. **Talking over it.** Barge-in is VAD-based, gated by the speaker check (`barge_in.py`). The brain interrupts the CLI
   and drains it, except while a Bash command runs, which is left to finish unheard (`_holding`).
6. **Unprompted speech.** `floor.py` holds announcements (task updates, heartbeat alerts, approval requests) while
   anyone is talking, JARVIS is thinking, words are on the way, or JARVIS's last words asked a question; then says them
   at once, in order. The heartbeat (`tasks/heartbeat.py`, every 15 minutes) flags meetings within 30 minutes, mail,
   Chat, GitHub and stalled tasks, with a model triaging the soft ones; the phone gets a push as well.

**What the logs already show.** In the conversation logs for 4 to 9 October, 230 of 1,235 replies (19%) were
"interrupted before replying": the turn ended at a pause, the brain started, and the user carried on, so the reply
was thrown away and the user's thought arrived in two or three pieces ("Okay, great. So... I think now... Let's do one
thing." then "Let's discuss what we should do next."). That is stage 2's problem, and the biggest single source of the
robotic feel. Stage 1's logs now measure it per turn (`ended: unspoken`, with `resumed`, the length of the pause).

---

## Stage 1: Measure every turn, and a lead-in that fits the job (done)

### Timing logs

`jarvis/voice/timing.py` times every turn that reaches JARVIS: one JSON line in `logs/voice-turns.jsonl` and a
readable `voice turn` line in `logs/agent.log`, once the reply is spoken or cut off. Each stage is seconds after the
user stopped speaking (LiveKit's VAD end of speech):

| Field | Meaning | Source |
|---|---|---|
| `transcript` | Whisper's text ready | LiveKit `transcription_delay` |
| `turn_end` | The turn detector decided they'd finished | LiveKit `end_of_turn_delay` |
| `brain` | `llm_node` started | JARVIS |
| `sent` | Words sent to the CLI (after any yes-or-no check, a command still running, photos) | `CliBrain.respond` |
| `first_text` | The reply's first words streamed | `llm_node` |
| `first_tool` | The model began its first tool call | `CliBrain.respond` (stream `content_block_start`) |
| `first_audio` | JARVIS's voice started | LiveKit (reply's `started_speaking_at`) |
| `done` | The model's reply ended | `CliBrain.respond` (`ResultMessage`) |

Plus `speech`, `words`, `tts_first` (Kokoro's first-audio time), `ack` (the words before the first tool call),
`tools`, `spoke`, and `ended`: `spoken`, `interrupted`, `unspoken` (the user went on before it was heard; with
`resumed`, how long they had paused) or `closed`.

Design choices: LiveKit already measures the user side and the audio side and attaches them to the chat messages
(`ChatMessage.metrics`), so the log reads them from `on_user_turn_completed` and `conversation_item_added` instead of
re-measuring. JARVIS's own marks go through a context variable (`timing.mark`, `timing.tool`), a no-op outside a timed
turn, so the brain stays usable without the voice agent. A record is written once, when its turn is over; nothing is
held across sessions. `make voice-timing` summarises: each stage's median, p90 and max, the waits between them (for
the turn to end, before the model, the model's first words, first words to voice), the pause before going on, how
many tool turns said something first, and the slowest turns.

### A lead-in that fits the job

The persona (`jarvis/prompts/persona.md`) already had the model say one short phrase before its first tool call. It
now asks for one that fits the job: none for a question it can answer straight away, a few words for a quick look, a
little more for slow work, worded afresh each time rather than repeating the last. The model decides, from the
request and what it's about to do; there is no list of phrases. The log's `ack` field and the summary's "turns with
tools said something first" show how it behaves.

**Not done, deliberately:** an acknowledgement spoken *before* the model's first token (a fast side call deciding a
phrase while Opus thinks). The model only knows whether a lead-in is needed once it has read the request, and a
separate caller would race it and risk saying "Checking your calendar" before a reply that also says it. If the logs
show `sent` to `first_text` is routinely more than about 1.5 s on tool turns, stage 3 revisits this (below).

---

## Stage 2: Don't cut the user off mid-thought

### Approach

1. **Look at the data first.** From `voice-turns.jsonl`: how many turns end `unspoken`, the spread of `resumed`
   pauses, and `turn_end` for those turns (did the detector think they were finished, ending at 0.9 s, or was it
   unsure and the pause outlasted 4 s?). That says whether the detector or the delays are wrong.

2. **Judge completeness on meaning, with a model, only when it's unclear.** Implement LiveKit's `_TurnDetector`
   protocol (`predict_end_of_turn(chat_ctx)`, `unlikely_threshold`) in a new `jarvis/voice/turn.py` that wraps the
   local v1-mini detector:
   - v1-mini clearly done (a high probability: "What's the time?"): end at once, with a short `min_delay`.
   - Clearly not done (very low: "Let's discuss what you"): wait the long delay.
   - In between: ask a fast model (Haiku, through the warm standby session `consent.py` already keeps) "has the user
     finished their thought, given the conversation so far?" with the last exchange and the words so far. Its answer
     becomes the probability. It runs during the silence that is being waited out anyway, so it costs time only when
     it's slower than that silence. Its verdict, the detector's, and what happened next go in the timing log.
   This is the model deciding, not word lists: neither "and..." nor "so" is special-cased.

3. **Adapt to each user's pace.** The `resumed` gaps of a user's `unspoken` turns are exactly how long they pause
   mid-thought; the gaps after spoken turns that were answered without complaint show when a quick reply was right.
   From these, per user, keep a running estimate (an exponential moving average, as LiveKit's own `dynamic`
   endpointing mode does, or a high percentile of the mid-thought pauses):
   - `max_delay` (the wait when the thought seems unfinished) a little above that user's usual mid-thought pause, so
     people who search for words or aren't speaking their first language get time;
   - `min_delay` (the wait after a finished-sounding turn) lowered for brisk speakers whose finished turns are never
     continued.
   Apply them with `session.update_options(endpointing_opts=...)` at session start and as the estimate moves. Stored
   per speaker in the `settings` table (keyed by voiceprint name, the owner by default), so each install learns its
   own users and nothing is tuned to one person. LiveKit's built-in `mode: "dynamic"` endpointing is a cheap first
   experiment before writing our own estimator.

4. **When it does cut in, recover gracefully.** Today an `unspoken` reply is thrown away and the continuation arrives
   as a new turn; the persona tells the brain to read the two together. Better: when the user goes on within a short
   time and before any audio, interrupt the CLI, and send the continuation marked as the rest of the same thought, so
   the model answers the whole of it. (The CLI's history already holds the first fragment.)

5. **Imperfect speech.** Restarts, fragments and misrecognised words:
   - Pass the recogniser's own confidence along: the speech gate already has Whisper's `avg_logprob` and `no_speech`
     per clip. A low-confidence turn reaches the brain with a short marker ("[heard with low confidence]").
   - Persona: when the words don't quite make sense or confidence is low, check back with a short best guess ("Did you
     mean the delay?") rather than "Say that again", and if the guess is cheap to act on, act on it and say so.
   - Per-user fluency, from numbers not words: how often their turns are low-confidence, restarted or continued, and
     their speaking rate. When those say the user finds English effortful, a line in the per-turn framing asks for
     shorter, simpler replies. The learning review (`tasks/learning.py`) already reads finished conversations and
     learns mishearings; it can also note, as a memory fact, how the user prefers to be spoken to.

### Files likely touched

`jarvis/voice/turn.py` (new: the wrapping detector and the per-user pace estimate), `jarvis/voice/agent.py` (use it;
`update_options`), `jarvis/core/config.py` (bounds for the delays), `jarvis/state/settings.py` or a small table in
`jarvis/core/database.py` (per-speaker pace), `jarvis/voice/speech.py` and `speech_gate.py` (confidence passed on),
`jarvis/brain/persona.py` and `prompts/persona.md` (low-confidence marker, checking back, simpler replies),
`jarvis/voice/timing.py` (log the detector's and model's verdicts), `jarvis/brain/cli.py` (continuation as one thought).

### Risks

- A model in the end-of-turn path adds latency and cost to every ambiguous turn. Keep it to the ambiguous band, keep
  its session warm, cap it with a timeout that falls back to v1-mini's verdict, and measure with stage 1's logs.
- Waiting longer for slow speakers makes JARVIS feel slow to them. The estimate must move both ways, and fast turns
  that are clearly finished must still end quickly.
- Per-speaker data needs the speaker check to know who is speaking; without a voiceprint everything falls back to the
  owner's estimate.
- Checking back too often is its own kind of robotic. The persona must keep it to genuinely unclear cases.

### What was built

- **Measuring first wasn't possible yet**: `logs/voice-turns.jsonl` doesn't exist on this Mac, because stage 1 hadn't
  run in the live services when this was built. The baseline is the conversation logs' 19% of replies thrown away
  (above); the timing log now records, per turn, `eot` (the detector's probability), `judge` and `judge_s` (the
  model's verdict and time), `wait` (short or long), `speaker` and `logprob`, and `make voice-timing` adds how often
  the user went on after each kind of wait. That is the before-and-after to read once it has run for a day or two.
- **The detector is an audio model, not a text one.** In LiveKit 1.8.3, `inference.TurnDetector(version="v1-mini")`
  is a streaming detector that hears the audio (`_StreamingTurnDetector`), not the `_TurnDetector` protocol that reads
  the chat context. So `jarvis/voice/turn.py`'s `JudgedTurns` wraps the streaming detector instead: each pause's
  prediction passes through `decide`. Under 0.05 or from 0.9 its verdict stands; in between it waits for Whisper's
  words for that pause (they arrive after the prediction starts) and asks the fast model, whose yes or no becomes 1 or
  0. The stream's `prediction_timeout` is raised to leave the model room, and a pause that ends in speech cancels its
  judgement. The agent tells it the words heard and JARVIS's last reply.
- **The warm session** is `jarvis/brain/quick.py`: one Haiku CLI session reused for up to 30 calls (unlike consent's
  one session per check, since it only judges timing), replaced when a call fails or hangs. A caller that gives up
  never leaves it mid-answer: the call is read to its end in the background, and a call still queued is skipped.
- **Pace** is our own estimator, not LiveKit's `dynamic` mode (which learns only `min_delay`, from pauses between
  utterances within a turn, and forgets each session). A cut-off moves the delay that ended it half way to the pause
  plus 0.3 s; each turn not continued lets it drift down (0.02 s for `min_delay`, 0.05 s for `max_delay`), within 0.4
  to 1.8 s and 2 to 8 s. Kept per speaker as `voice_pace:<speaker>` in the settings table.
- **Imperfect speech**: Whisper's `avg_logprob` for each clip passed on is kept (`WhisperSTT.take_clips`, with the
  voiceprint the speaker check matched), and a turn under -1.0 reaches the brain marked as unsure. A turn that goes on
  from an unheard reply is marked with the earlier words, so the model answers the whole thought even if the first
  part never reached the CLI. Fluency is three running numbers per speaker (share of low-confidence turns, share
  carried on after, words a second); past 10 turns, when one passes its line, the turn asks for plain replies. The
  persona checks back with a short best guess instead of "say that again". The learning review may note how the user
  likes to be spoken to.

---

## Stage 3: Speak as it thinks (about one second to first audio)

### Where the time goes now

Sentence-by-sentence streaming to Kokoro already exists (`speech.streaming`, `clauses.py`), so this stage is about
the first sentence. Stage 1's waits break a turn into: waiting for the turn to end (at least `min_delay`, 0.9 s, after
the VAD's own silence), before the model (consent check, a command still running, photos), the model's first words
(the CLI and Opus at effort `low`; the persona, memory and recent conversation are in a cached system prompt), and
first words to voice (the tokenizer waiting for a 20-character sentence plus context, then Kokoro's render of the
first piece). A one-second target is only reachable if every one of those shrinks, so the order is set by the data.

### Approach

1. **End of turn sooner on finished questions** (stage 2's semantic detector lets `min_delay` drop well under 0.9 s
   when the model is sure).
2. **Shorter first piece.** Let the first piece of a reply go at its first clause or a short sentence (lower
   `MIN_SENTENCE_CHARS` and `MIN_WORDS` for the first piece only, in `ClauseTokenizer`), and ask in the persona for a
   short first sentence that leads with the answer. Kokoro renders a 4-word piece in a fraction of the time of a
   12-word one.
3. **Faster first token.** Measure `sent` to `first_text` by turn type. Options, in order of cost: keep the CLI warm
   between turns (already one process per session); make sure the system prompt stays cache-friendly (nothing that
   changes per turn ahead of stable text); try effort `low` vs. none for the voice model; let the model route simple
   turns to a faster model (a lighter voice model for chit-chat and short factual answers, the main one when tools or
   reasoning are needed), decided by a fast model rather than rules.
4. **If still slow, an early acknowledgement** (stage 1's deferred idea): when the main model hasn't produced text
   within a set time, a fast model that has read the request may say a short fitting phrase, and the main reply is
   told it was said, so it doesn't repeat it.
5. **Preemptive generation** (LiveKit's, now off because speculative turns would write half-heard text into the CLI's
   history) only if the above isn't enough, and only in a throwaway session that doesn't touch the main history.

### Files likely touched

`jarvis/voice/clauses.py`, `jarvis/voice/speech.py` (first-piece tokenizing), `jarvis/prompts/persona.md`,
`jarvis/brain/cli.py` and `jarvis/brain/sdk.py` (model routing, prompt layout), `jarvis/core/config.py`,
`jarvis/voice/timing.py` (first piece's length and render time).

### Risks

- Shorter first pieces can sound choppy (each piece has its own intonation); tune against recordings.
- A lighter model for "simple" turns answers some of them worse; the routing must be conservative, and the
  conversation history must stay in one place so switching models doesn't lose context.
- A second acknowledgement source can double up with the model's own phrase.

### What was built

- **Where the floor is.** Reading the code shows a hard floor under every reply: Silero's END_OF_SPEECH needs
  `min_silence_duration` (0.55 s) of silence before Whisper (batch, about 0.4 s) starts, and LiveKit only ends a turn
  once Whisper's text is in. So a turn can't end before about 0.95 s, whatever `min_delay` says, and first audio about
  one second after a simple question isn't reachable with batch Whisper and the CLI's Opus; what's reachable is
  shrinking every step after that. `JARVIS_VAD_MIN_SILENCE` (default unchanged, 0.55 s) is now the lever for the
  first step: with stage 2's model guarding against cut-offs, 0.4 s is worth trying, at the cost of more fragments.
- **Shorter first piece**: `ClauseTokenizer`'s stream lets a reply's first piece go at 8 characters instead of 20 and
  cuts a first sentence of over 6 words at its first pause; later pieces are cut as before. The persona asks for a
  short first sentence that leads with the answer. The log's `piece` is the first piece's length in words.
- **A lighter model for simple turns** (`jarvis/brain/route.py`), opt-in with `JARVIS_VOICE_QUICK_MODEL` (say
  `claude-sonnet-5`): a fast model reads the words as soon as they're transcribed, during the end-of-turn wait, and
  routes small talk and short general-knowledge questions to the lighter model, everything else (anything touching the
  user's data, tools, actions or reasoning, or unclear) to the main one. The reply waits at most 0.4 s for the verdict.
  Both answer in the same CLI session (`set_model` per turn), so history stays in one place. Off by default because
  each model keeps its own prompt cache, so switching may cost more than it saves; the log's `model` field and the
  summary's "first audio, turns with no tools" (main against lighter model) settle it.
- **Not built**: the early acknowledgement from a second model and preemptive generation. Both were "only if still
  slow"; with no timing data yet they would add a second voice and the risk of doubled phrases for an unmeasured gain.
  The summary's "model to first words" on tool turns says whether they're needed.

---

## Stage 4: Slow work carries on in the background

### Today

The persona sends multi-step work to background tasks (`task_start`), and long shell commands to command jobs
(`start_job`); both announce their results through the floor. But anything slow inside a turn (several web searches,
a slow MCP call, a lookup subagent) blocks the conversation: the user waits in silence after the lead-in, and if they
speak, the reply is interrupted and the work is lost (only a running Bash command is kept, via `_holding`).

### Approach

1. **Measure it.** Stage 1's `first_tool` to `done` per turn says how often in-turn work is slow, and which tools.
2. **Hand over instead of throwing away.** When the user speaks over a turn that's busy in a tool call (not only Bash),
   let that work finish in the background, as `_finish` already does for Bash, and give the conversation a fresh turn
   at once. The result comes back as an announcement through the floor ("The flight search finished: ..."), and the
   brain is told about it like any other note. This needs the conversation to continue while the CLI session is busy,
   so it means a second CLI session for the conversation (forked from the first, `fork_session`/`resume`), or a
   short-lived side session for the slow work, which then reports back.
3. **Let the model choose up front.** A tool the voice brain can call, "do this in the background and tell me", for
   work that will plainly take more than a few seconds but doesn't need a full task (a few searches, a long read). It
   runs on a cheaper model with the same read tools, in the runner, and comes back as an announcement. The model
   decides when to use it; the persona already says when work is too big for a turn.

### Files likely touched

`jarvis/brain/cli.py` (handing a busy turn over, a second session), `jarvis/tools/tasks.py` and
`jarvis/tasks/runner.py` (a light "side job" kind, or reuse of `research`), `jarvis/voice/floor.py` (announcing side
job results), `jarvis/prompts/persona.md`.

### Risks

- Two CLI sessions cost memory on an 8 GB Mac (each is a Node process); the side session should be short-lived and
  on a smaller model.
- Two sessions can diverge: the conversation must be told what the background work found, and the background work
  must not act on anything the user has since changed their mind about.
- Approval questions from background work must still go through the existing floor and consent path.

### What was built

- **Measuring**: the log now names each turn's tools (`tool_names`), and `make voice-timing` gives the spread from
  the first tool call to the reply's end, and how many turns handed work to a background lookup.
- **The model chooses up front** (step 3): `background_lookup` (`jarvis/tools/side.py`, `jarvis/brain/side.py`) runs
  a side job on `JARVIS_SIDE_MODEL` (Sonnet by default) with web search and fetch and JARVIS's read-only tools (the
  Google and Mac reads, conversation search), `permission_mode="dontAsk"`, so it can't change anything and never
  needs an approval. It runs in the voice agent's process for that conversation, at most two at once and three
  minutes each (`JARVIS_SIDE_MINUTES`), as a short `query()` that ends with it, not a second long-lived CLI. Its
  answer is an announcement through the floor, and the brain hears it like any other. The persona and the tool's
  description say when: reading that would keep the user waiting more than a few seconds but isn't a whole task.
- **Hand over instead of throwing away** (step 2), the light version: when the user speaks over a turn busy in a
  tool call other than Bash (which is still left to finish, as before), the reply is stopped as before, and the next
  turn tells the model what it was working on and that it may hand it to `background_lookup` if it's still wanted.
  The model decides, so "never mind" drops it. Letting the interrupted call itself run on would need a second CLI
  session for the conversation, forked mid-turn with a tool call unanswered; on an 8 GB Mac that's another Node
  process for every talked-over turn, and resuming a session mid-tool-call isn't something the SDK promises, so it's
  not done.

---

## Stage 5: Proactivity at the right moment

### Today

JARVIS speaks unprompted for task updates, heartbeat alerts (including meetings within 30 minutes) and approval
requests. The floor holds them only while an exchange is under way or a question is open, then says them at once, in
order; the phone gets a push too. There is no notion of urgency, of a deep discussion, or of saving an item for later.

### Approach

1. **Give each item a weight.** Sources already know a lot: a meeting starting in 2 minutes, a task that needs a yes,
   a task finished, a newsletter. The heartbeat's triage model and the runner attach an urgency (urgent, soon, whenever)
   and a "still relevant until" time.
2. **Let a model judge the moment.** When an item is waiting and the floor is free, a fast model sees the item, its
   urgency, and the state of the conversation (the last few exchanges, how long since anyone spoke, whether JARVIS's
   last words were a question, whether the user is mid-discussion of something else) and picks one of:
   - **now**: a natural pause, or the item is urgent;
   - **at the next pause**: deep in a discussion; hold it until the topic closes;
   - **later**: wait for the next conversation, or the greeting;
   - **phone**: the conversation is over; push it instead.
   It returns its choice, and the floor acts on it. No timers or word lists decide what "deep in discussion" means.
3. **Never talk over the user.** That rule stays absolute in the floor. Only urgent items may cut into a discussion,
   and even they wait for the end of the user's sentence (the VAD's silence), then say a short "Sorry to cut in: ...".
4. **Follow-ups.** The learning review already reads finished conversations; it can also note follow-ups the user
   mentioned ("remind me to check the build after lunch") as scheduled reminders (the scheduler exists), which then
   come back through the same judged floor.
5. **Upcoming meetings** come from the heartbeat's calendar check; the judged floor decides whether to mention one
   during a conversation now or a few minutes before it starts.

### Files likely touched

`jarvis/voice/floor.py` (urgency, the model's timing decision, deferral), `jarvis/voice/agent.py` (`watch_tasks`),
`jarvis/tasks/heartbeat.py` and `jarvis/prompts/heartbeat_triage.md` (urgency), `jarvis/notify/push.py` (phone for
items the conversation didn't take), `jarvis/tasks/learning.py` (follow-ups), `jarvis/voice/timing.py` (log when
items were said, held or pushed).

### Risks

- A model call per waiting item costs money and time; batch items and call only when the floor is free and something
  is waiting.
- Holding items too long makes them stale, or lost; every held item needs an expiry that falls back to the phone.
- Interruptions for "urgent" items must stay rare, or users stop trusting them. Log every break-in so it can be tuned.

### What was built

- **Weights**: each `Announcement` carries `urgency` (urgent, soon, whenever), `at` (the time it's about) and
  `pushed` (its source already sent it to the phone). The heartbeat's triage model can now mark a kept item urgent
  (`3!`), and a calendar alert carries the meeting's start; an item counts as urgent once its time is within 5
  minutes, and is dropped 5 minutes after. Reminders and task updates come as "soon".
- **The judged floor** (`jarvis/voice/floor.py`, `Moment`): once nothing holds the floor (the existing holds stay
  absolute, urgent items included, so nothing talks over the user or into an open question), a fast model on a warm
  session sees the waiting items and the conversation's last lines and silence, and picks now, cut_in, pause or phone
  for each. Phone was kept and "later" folded into it, because nothing but a task's update survives a session: a task
  left unheard stays unseen, so the next greeting mentions it; anything else goes to the phone unless its source sent
  it already. Pause is asked again after the next exchange or a 45-second quiet, never in a loop; held 15 minutes, it
  goes to the phone, as does whatever is left when the session ends. cut_in is honoured only for urgent items, said
  after "Sorry to cut in:", and logged every time. An item said 3 minutes or more after it came says how long ago, so
  "starts in 20 minutes" doesn't mislead. With `JARVIS_PROACTIVE_JUDGE=off`, or when the call fails, a free floor
  says items at once, as before.
- **Follow-ups**: the learning review may suggest `follow_up` (text and a local time) for something the user said
  they'd see to later; it's a new data edit that always needs their yes, and becomes a scheduled reminder, which
  comes back through the same judged floor, or the phone.
- **Upcoming meetings**: the heartbeat still flags them within 30 minutes; the floor now decides whether to mention
  one during a discussion now or let it escalate as the start nears.
- Not done: the model's decisions aren't in `voice-turns.jsonl` (they aren't turns); held, said, cut-in and pushed
  items are each a line in `logs/agent.log`.

---

## Reading the timing logs

```
make voice-timing            # the last 24 hours
make voice-timing HOURS=2    # the last two
```

The summary's first line counts how turns ended. A high share of `unspoken` means JARVIS is answering before the user
has finished (stage 2); the "pause before going on" line shows how long those pauses were. "Where the wait goes"
breaks the time to first audio into waiting for the turn to end, work before the model, the model's first words, and
first words to voice; whichever is biggest is the next thing to fix. Single turns are in `logs/voice-turns.jsonl`
(one JSON object a line) and, readably, in `logs/agent.log`:

```
voice turn 4 (1.5 s of speech, 5 words): transcript 0.30, turn end 0.90, brain 0.95, sent 1.00, first text 2.20,
first tool 2.20, first audio 2.70, done 4.50; 2 tools; spoken; ack 'Checking your calendar.'
```
