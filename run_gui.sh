#!/usr/bin/env bash
set -eu

project_dir="$(cd -- "$(dirname -- "$0")" && pwd)"
python_cmd="$project_dir/.venv/bin/python"
if [[ ! -x "$python_cmd" ]]; then
    python_cmd="python3"
fi
exec "$python_cmd" "$project_dir/wifi_gui.py"
