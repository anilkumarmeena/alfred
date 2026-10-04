"""JARVIS, a personal voice assistant that runs on one Mac. Each folder here is one part; README.md maps them."""

import os

# onnxruntime otherwise sends Microsoft telemetry once a model loads. Set before any does, so every JARVIS process and
# the job processes it starts inherit it; ORT_DISABLE_TELEMETRY=0 in the environment turns it back on.
os.environ.setdefault("ORT_DISABLE_TELEMETRY", "1")
