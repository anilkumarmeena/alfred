"""The task runner's process (the tasks service runs python -m alfred.tasks.runner, which starts this): a runner wired
to the user's other machines (alfred.node), served on the runner socket until stopped."""

import asyncio
import logging

from alfred import bootstrap
from alfred.node import offload
from alfred.tasks import runner


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    bootstrap.wire()
    asyncio.run(runner.serve(runner.Runner(machines=offload.wire)))


if __name__ == "__main__":
    main()
