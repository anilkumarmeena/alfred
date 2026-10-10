"""JARVIS: a personal voice assistant that runs on one Mac. docs/development.md maps the packages."""

import os

# Opt out of onnxruntime telemetry before any model loads; children inherit it. ORT_DISABLE_TELEMETRY=0 re-enables.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
