# 🖥️ Machines: sending heavy work to a second computer

JARVIS can use another computer of yours for heavy work: renders, big builds and Claude tasks. That computer runs
JARVIS in **node mode** (`python -m jarvis.node`), with its own Claude account, and works under the same walls as the
main computer.

## How it behaves

| | |
|---|---|
| **Pairing** | Settings › Machines › *Add a machine* shows a one-time code (10 minutes). On the other computer: `make node-pair CODE=… MAIN=https://<main computer>`. Both screens show fingerprints; confirm in the panel. Never by voice, and only from the owner's login (the first login made; Settings › Logins and keys marks it): other logins see the machines and where jobs run, but can't make a code, confirm or forget a machine, or change or remove the owner's login. |
| **The link** | The machine dials the main computer (WebSocket to `/api/node`, passed to the task runner). Both sides prove who they are with Ed25519 keys, and every request is signed for that session. Nothing listens on the machine. |
| **Online check** | Every job starts with a ping answered within 5 s: free slots, load, code version, its Claude usage. No answer means offline. |
| **Where jobs run** | Per user, per kind (long commands, Claude tasks): this computer, a machine, or *let JARVIS choose*. A newly paired machine sets every unset kind to *let JARVIS choose*. Research and tasks that read accounts or other projects always stay on the main computer. The user can say where one job runs ("run this one here"). |
| **Let JARVIS choose** | Every paired machine is checked at once. Those able to take the job (online, same protocol, the programs a command runs, and for a Claude task a free slot and Claude usage left) go to `JARVIS_PLACEMENT_MODEL` with this computer, each with its cores, load, free slots and share of its Claude limit left. If the model can't be asked, the computer with the most room takes it (a free slot and usage left first for a task, then idle cores), this one on a tie. |
| **Fallbacks** | Offline, busy, updating, out of Claude usage, or missing a program the command runs: the job runs on the main computer and JARVIS says why. When the main computer can't take it (its Claude usage is used up, or it lacks the program), it goes to the able machine with the most room instead. |
| **Copies** | A project is copied to the machine: files by hash (secrets, `.git` and folder-bound caches never go), history as a git bundle. Afterwards the changed files come back; a file also changed here meanwhile is kept beside it as `name.from-<machine>`. |
| **Skills** | A run there loads JARVIS's skills as switched on here: each run names them by version, the machine fetches a version it lacks once, checks it against that version's digest and the skill rules again, and keeps it in `data/node/skills/` (pruned after 14 days unused). Its own skills table is never used. |
| **Same gates** | Tasks run the same `worker.run`, sandbox, file gate and host walls there. A risky command waits for a yes asked on the main computer (described there from the command itself); the machine also refuses a risky job command without the main computer's word that the user agreed. Keys, accounts and push stay on the main computer. |
| **Reports** | Every step, question and approval request goes through the machine's journal and is replayed in order after a dropped link. The main computer's database stays the one record; JARVIS names the machine in replies, task cards and announcements. |
| **Restarts** | A run still going on the machine is taken up again after the main computer restarts; a run cut off by the machine restarting carries on in its own session. |
| **Unreachable** | After 2 minutes the bell says so; after `JARVIS_NODE_LOST_MINUTES` (30) one notice (spoken, and a push). A task whose session lives there waits `JARVIS_NODE_WAIT_MINUTES` for it. |
| **Updates** | On connecting, a machine on another commit waits until it's idle, fetches a bundle of the main computer's commit over the link, installs its locked dependencies and restarts. If the new code fails to start 3 times it rolls back and tells the main computer once. |

## Decisions

1. A JARVIS worker on the second computer, not SSH.
2. Not always on: the online check comes first.
3. Its own Claude account.
4. Where each kind of job runs is a per-user setting.
5. Moving whole agents there for good: deferred.
6. While the main computer is off, the machine keeps running its own projects' scheduled jobs: commands and Claude
   tasks.
7. Work is sent without asking, but JARVIS always says where it runs.
8. The machine updates itself to the main computer's code.
9. Machine offline: heavy jobs run on the main computer instead.
10. A newly paired machine: placement starts as *let JARVIS choose*.
11. A scheduled task on the machine that needs a yes while the main computer is off **waits** for it, never skips.
12. The machine **waits until idle** before updating.

## Setting up the second computer

```sh
make node-setup      # Python dependencies only
make node-login      # its own Claude account
make node-pair CODE=… MAIN=https://<main computer>   # then confirm the fingerprint in Settings › Machines
make node-install    # the service (launchd or systemd); it keeps the computer awake only while working
make node-check      # walls, pairing, link, tools
```

## Settings

`JARVIS_PLACEMENT_MODEL`, `JARVIS_NODE_LOST_MINUTES` (30), `JARVIS_NODE_WAIT_MINUTES` (30), `JARVIS_NODE_SYNC_MAX_GB` (20).

## Projects that live on a machine

Say so when adding it ("the film project lives on the studio Mac at ~/projects/film"): the brain passes `machine` to
`project_register`, and that machine checks the folder (in its home, not hidden, never a secrets folder or JARVIS's
own) and makes it if missing. From then on its tasks and long commands always run there, in its own folder, whatever
the placement setting: in place, or on a branch in a worktree there. The git steps (saving uncommitted work under
`refs/jarvis/`, worktrees, commits, merge and discard) run on that machine (`git_ops.OPS`, sent over the link). Nothing
is copied. If the machine is away, the task waits for it (`JARVIS_NODE_WAIT_MINUTES`); it never runs here.

## Scheduled jobs of projects that live on a machine

That machine owns them, so none runs twice: the main computer sends their definitions, do-not-disturb and switched-on
skills on every connect and whenever they change, and skips them itself. The machine starts each on time whether the
main computer is on or not, and keeps its own record of when each last ran.

- **Commands** it runs itself and reports through its journal; the main computer records each as a command task and
  updates the job.
- **Claude tasks** (code changes and questions; research stays on the main computer) it hands to the main computer
  when the link is up (`schedule.due`), which starts the task there as its scheduler would; it then runs on the
  machine like any task of that project. A hand-over whose answer is lost is asked again, never begun alone, and the
  main computer answers a repeated ask with the same task.
- **While the main computer is away**, the machine begins a due task alone, as the main computer would (on a branch in
  a worktree, or in the project's folder with uncommitted work saved under `refs/jarvis/` first), under a stand-in id,
  with its own Claude account's usage hold and the skills last sent. A risky command waits for the user's yes, asked
  once the main computer is back. On return the run is reported (`away`): the main computer records it as the task it
  is, gives it its id, and takes it up as after a restart (following it if still going), then commits, checks and
  reports it as usual.

Runs reported after the main computer was away come together in one summary (spoken, and one push); a run that needs
the user (a question, a yes) is said on its own.

## Not built yet

- The goal check's diff evidence for runs on a machine.
- Allowed hours per machine.
