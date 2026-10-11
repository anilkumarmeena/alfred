"""Other machines JARVIS sends heavy work to: a second computer that runs JARVIS in node mode.

Main computer (this package's main-side modules, used by the task runner):
  machines   the paired machines, pairing codes, and the per-user "where jobs run" setting
  hub        the machines' links: sign-in, online check, sending work, updates
  placement  choosing where a job runs
A machine (python -m jarvis.node):
  agent      dials the main computer, runs what it's sent with the same gates as here
  home       its own files: key, pairing, journal
Shared: keys (Ed25519), link (signed requests over a WebSocket), sync (files), bundles (git).
"""
