#!/bin/bash
set -e

# `docker run <image> serve` boots the WebUI hub (dynamic channels, status,
# recordings library) instead of the plain CLI recorder. Everything else is
# passed straight to main.py, so existing CLI usage is unchanged.
if [ "$1" = "serve" ]; then
    shift
    exec /app/.venv/bin/python serve.py "$@"
else
    exec /app/.venv/bin/python main.py -no-update-check "$@"
fi
