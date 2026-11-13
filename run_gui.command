#!/bin/zsh
set -eu

project_dir="${0:A:h}"
exec python3 "${project_dir}/wifi_gui.py"
