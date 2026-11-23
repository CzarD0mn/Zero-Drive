#!/usr/bin/env bash
set -euo pipefail
python3 -m py_compile zerodrive.py
bash -n zd-helper
