#!/usr/bin/env bash
set -euo pipefail

python3 -m compileall main.py motionxbot
echo "Python compile check passed."
