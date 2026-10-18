#!/bin/sh
# Source-built installer for macOS and Linux. Native Claude and Git must already be installed.
set -eu
case "$(uname -s)" in
  Darwin|Linux) ;;
  *) echo 'Claude Pool supports macOS and Linux only.' >&2; exit 1 ;;
esac
task_python=''
for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,11))' 2>/dev/null; then
    task_python=$(command -v "$candidate")
    break
  fi
done
if [ -z "$task_python" ]; then
  if [ "$(uname -s)" != Darwin ]; then
    echo 'Install Python 3.11+ (Debian/Ubuntu: apt install python3), then rerun this command.' >&2
    exit 1
  fi
  if ! command -v brew >/dev/null 2>&1; then
    echo 'Install Python 3.11+ (or Homebrew), then rerun this command.' >&2
    exit 1
  fi
  brew install python@3.13
  task_python="$(brew --prefix python@3.13)/bin/python3.13"
fi
command -v git >/dev/null 2>&1 || { echo 'Install Git, then rerun.' >&2; exit 1; }
task_stage=$(mktemp -d "${TMPDIR:-/tmp}/claude-pool-setup.XXXXXX")
trap 'rm -rf "$task_stage"' EXIT HUP INT TERM
git clone --quiet --depth 1 https://github.com/justusaugust/claude-pool.git "$task_stage/source"
"$task_python" "$task_stage/source/install.py" --bootstrap "$@"
