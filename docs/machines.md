# 🖥️ Machines: sending heavy work to a second computer

JARVIS can use another computer of yours for heavy work: renders, big builds and Claude tasks. That computer runs
JARVIS in **node mode** (`python -m jarvis.node`), with its own Claude account, and works under the same walls as the
main computer.

## How it behaves

| | |
|---|---|
| **Pairing** | Settings › Machines › *Add a machine* shows a one-time code (10 minutes). On the other computer: `make node-pair CODE=… MAIN=https://<main computer>`. Both screens show fingerprints; confirm in the panel. Never by voice. |
| **The link** | The machine dials the main computer (WebSocket to `/api/node`, passed to the task runner). Both sides prove who they are with Ed25519 keys, and every request is signed for that session. Nothing listens on the machine. |
| **Online check** | Every job starts with a ping answered within 5 s: free slots, load, code version, its Claude usage. No answer means offline. |
| **Where jobs run** | Per user, per kind (long commands, Claude tasks): this computer, a machine, or *let JARVIS choose*. A newly paired machine sets every unset kind to *let JARVIS choose*. Research and tasks that read accounts or other projects always stay on the main computer. The user can say where one job runs ("run this one here"). |
| **Fallbacks** | Offline, busy, updating, or out of Claude usage: the job runs on the main computer and JARVIS says why. When the main computer's Claude usage is used up, a Claude task goes to a machine that can take it. |
| **Copies** | A project is copied to the machine: files by hash (secrets, `.git` and folder-bound caches never go), history as a git bundle. Afterwards the changed files come back; a file also changed here meanwhile is kept beside it as `name.from-<machine>`. |
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
6. While the main computer is off, the machine keeps running its own projects' scheduled jobs (phase 4).
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

## Scheduled commands of projects that live on a machine

That machine owns them, so none runs twice: the main computer sends their definitions and do-not-disturb on every
connect and whenever they change, and skips them itself. The machine runs each on time whether the main computer is on
or not, keeps its own record of when each last ran, and reports every run through its journal; the main computer
records it as a command task and updates the job. Runs reported after the main computer was away come together in one
summary (spoken, and one push).

## Not built yet

- Scheduled Claude tasks of projects on a machine still start from the main computer's scheduler (they then run on
  the machine, and a risky command there waits for the user's yes); while the main computer is off they wait for it.
- JARVIS's own skills shipped to runs on the machine; the goal check's diff evidence for runs there.
- Restricting pairing to the owner's login (today any panel login can pair a machine).
