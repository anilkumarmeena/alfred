"""Alfred: a personal voice assistant that runs on one Mac or Linux machine. docs/development.md maps the packages."""

import os

from alfred.core import platform as _platform

# Before any Unix-only import (pwd, fcntl) fails with an ImportError on native Windows
_platform.require_supported()

# Opt out of onnxruntime telemetry before any model loads; children inherit it. ORT_DISABLE_TELEMETRY=0 re-enables.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
