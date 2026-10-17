"""Run this checkout as a machine the main computer sends work to.

  uv run python -m jarvis.node pair CODE https://main.example.ts.net   pair, with a code from Settings › Machines
  uv run python -m jarvis.node serve                                   dial the main computer and work (the service)
  uv run python -m jarvis.node check                                   walls, pairing, link, tools
  uv run python -m jarvis.node status                                  pairing and fingerprints
"""

import sys
from pathlib import Path

from jarvis.node import update

ROOT = Path(__file__).resolve().parents[2]


def _serve() -> int:
    if said := update.settle(ROOT):  # first: a broken update must still be able to roll back
        print(said, file=sys.stderr)
    import asyncio
    import logging
    import signal

    from jarvis import bootstrap
    from jarvis.node import agent, home, work

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    bootstrap.wire()
    pairing = home.pairing()
    if not pairing:
        print("This machine isn't paired yet: make node-pair CODE=... MAIN=...", file=sys.stderr)
        return 1

    async def main() -> None:
        node = agent.Agent(int(pairing["node"]), home.key(), pairing["main_public"],
                           lambda: agent.websocket_dial(pairing["main"]))
        jobs = work.Work(node)  # registers the work handlers
        jobs.prune()  # old copies go
        jobs.resume()  # runs a restart here cut off carry on
        asyncio.create_task(jobs.schedule_loop())  # its projects' scheduled commands, main computer on or not
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, node.stop)
        await node.run()

    asyncio.run(main())
    return 0


def _pair(code: str, main: str) -> int:
    import asyncio
    import json
    import urllib.error
    import urllib.request

    from jarvis.node import bundles, home, keys

    public = keys.public(home.key())
    facts = home.facts(asyncio.run(bundles.head(ROOT)))
    request = urllib.request.Request(f"{main.rstrip('/')}/api/node/pair", method="POST",
                                     data=json.dumps({"code": code, "public_key": public, "facts": facts}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            got = json.loads(reply.read())
    except urllib.error.HTTPError as e:
        try:
            why = json.loads(e.read()).get("error") or e.reason
        except ValueError:
            why = e.reason
        print(f"Not paired: {why}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError) as e:
        print(f"Couldn't reach {main}: {e}", file=sys.stderr)
        return 1
    home.save_pairing(int(got["node"]), main, got["main_public"], got["name"])
    print(f"Paired as {got['name']}.\n"
          f"  This machine's fingerprint:     {keys.fingerprint(public)}\n"
          f"  The main computer's fingerprint: {keys.fingerprint(got['main_public'])}\n"
          "Check that Settings › Machines on the main computer shows the same two, and confirm there. Then start the "
          "service: make node-install")
    return 0


def _status() -> int:
    from jarvis.node import home, keys

    pairing = home.pairing()
    if not pairing:
        print("Not paired: make node-pair CODE=... MAIN=...")
        return 1
    print(f"Paired as {pairing.get('name')} with {pairing['main']}\n"
          f"  This machine's fingerprint:     {keys.fingerprint(keys.public(home.key()))}\n"
          f"  The main computer's fingerprint: {keys.fingerprint(pairing['main_public'])}")
    return 0


def _check() -> int:
    import asyncio

    from jarvis.apps import host_check
    from jarvis.node import agent, bundles, home, link

    good = True
    print("Walls around tasks' commands:")
    good &= asyncio.run(host_check.check())
    pairing = home.pairing()
    print(f"{'ok  ' if pairing else 'FAIL'}  paired" + (f" with {pairing['main']}" if pairing else ": make node-pair"))
    good &= bool(pairing)
    if pairing:
        async def sign_in() -> str:
            channel = await agent.websocket_dial(pairing["main"])
            try:
                _, welcome = await link.handshake_machine(channel, int(pairing["node"]), home.key(),
                                                          pairing["main_public"], home.facts())
            finally:
                await channel.close()
            mine = await bundles.head(ROOT)
            return "same code as the main computer" if welcome.get("commit") == mine else \
                f"on {mine[:7]}, the main computer on {str(welcome.get('commit'))[:7]}: it updates itself when idle"

        try:
            said = asyncio.run(sign_in())
            print(f"ok    signs in to the main computer; {said}")
        except Exception as e:
            good = False
            print(f"FAIL  signs in to the main computer: {e}")
    print(f"note  tools found: {', '.join(home.facts()['tools']) or 'none'}")
    return 0 if good else 1


def main(argv: list[str]) -> int:
    match argv:
        case ["serve"]:
            return _serve()
        case ["pair", code, main_url]:
            return _pair(code, main_url)
        case ["status"]:
            return _status()
        case ["check"]:
            return _check()
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
