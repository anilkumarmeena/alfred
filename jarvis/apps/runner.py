"""The task runner's process (the tasks service runs python -m jarvis.tasks.runner, which starts this): a runner wired
to the user's other machines (jarvis.node), served on the runner socket until stopped."""

import asyncio
import logging

from jarvis import bootstrap
from jarvis.node import offload
from jarvis.tasks import runner


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    bootstrap.wire()
    asyncio.run(runner.serve(runner.Runner(machines=offload.wire)))


if __name__ == "__main__":
    main()
