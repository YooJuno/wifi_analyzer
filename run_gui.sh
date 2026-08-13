#!/usr/bin/env bash
set -eu

project_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
exec python3 "$project_dir/wifi_gui.py"
