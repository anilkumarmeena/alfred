"""The services' entry points from before the rename to Alfred, so services installed then keep starting (a second
machine updates its code itself, then restarts under its old service) until setup rewrites them under their new labels
(alfred.setup_local.migrate_legacy). Each hands over to the alfred module of the same name. Remove once no install
from before the rename is left."""

import os
import sys


def hand_over(module: str) -> None:
    """Replace this process with `python -m <module>`, keeping the arguments."""
    os.execv(sys.executable, [sys.executable, "-m", module, *sys.argv[1:]])
