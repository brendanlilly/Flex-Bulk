#!/usr/bin/env bash
# Flex Bulk — one-command setup and start for macOS and Linux.
#   bash start.sh              first run: installs Python 3.11 + packages into ./.venv, loads the item files, opens the browser
#   bash start.sh --rebuild    load and index the item files again
# The offline (jump-drive) copy carries uv, Python 3.11 and every package in offline/<platform>/, so it installs
# without internet; other copies download them (uv from astral.sh, packages from PyPI).
# Everything stays inside this folder (plus uv's cache). Delete the folder to uninstall.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# 0. the offline kit for this machine, if this copy has one
OFF=""
case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  [ -d offline/mac-arm64 ] && OFF=offline/mac-arm64 ;;
  Darwin-x86_64) [ -d offline/mac-x64 ] && OFF=offline/mac-x64 ;;
esac

if [ -n "$OFF" ]; then
  # copied from a zip that came from a download or email: macOS would block the bundled programs
  xattr -dr com.apple.quarantine . 2>/dev/null || true
  chmod +x "$OFF/uv" "$OFF"/python/bin/* 2>/dev/null || true
  UV="$PWD/$OFF/uv"
  PY="$PWD/$OFF/python/bin/python3.11"
  REQ="$OFF/requirements.txt"
  export UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never UV_NO_CONFIG=1
  INSTALL=(--offline --no-index --find-links "$OFF/wheels")
  say "Offline install kit: $OFF"
else
  # 1. uv (Astral's Python installer/package manager): reuse if present, otherwise install to ~/.local/bin
  UV=$(command -v uv || true)
  for c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" /opt/homebrew/bin/uv /usr/local/bin/uv; do
    [ -z "$UV" ] && [ -x "$c" ] && UV="$c"
  done
  if [ -z "$UV" ]; then
    say "Installing uv (Python installer from astral.sh, ~40 MB, goes in ~/.local/bin) ..."
    curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh
    UV="$HOME/.local/bin/uv"
  fi
  PY=3.11
  REQ=requirements.txt
  INSTALL=()
fi

# 2. Python 3.11 virtual environment + packages (skipped when the package list hasn't changed).
#    A folder moved after the first run leaves .venv pointing at the old place: start it over.
if [ -e .venv ] && ! .venv/bin/python -c "" 2>/dev/null; then
  say "The Python environment was made in another place; recreating it ..."
  rm -rf .venv
fi
if [ ! -x .venv/bin/python ]; then
  say "Creating the Python 3.11 environment ..."
  "$UV" venv --python "$PY" .venv
fi
REQ_HASH=$(cksum "$REQ" | awk '{print $1}')
if [ "$(cat .venv/.req-hash 2>/dev/null || true)" != "$REQ_HASH" ]; then
  say "Installing packages (first run only, ~1-2 min) ..."
  "$UV" pip install --python .venv/bin/python ${INSTALL[@]+"${INSTALL[@]}"} -r "$REQ"
  echo "$REQ_HASH" > .venv/.req-hash
fi

# 3. macOS: LightGBM's wheel expects Homebrew's libomp. scikit-learn ships the same library,
#    so point the loader at that copy instead of requiring Homebrew.
if [ "$(uname)" = "Darwin" ]; then
  SKL=$(.venv/bin/python -c "import sklearn, os; print(os.path.join(os.path.dirname(sklearn.__file__), '.dylibs'))")
  export DYLD_FALLBACK_LIBRARY_PATH="$SKL${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}"
  if ! .venv/bin/python -c "import lightgbm" 2>/dev/null; then
    echo "LightGBM could not load its OpenMP library (libomp). Install it with: brew install libomp" >&2
    exit 1
  fi
fi

# 4. load the item files when new, then serve
exec .venv/bin/python -m flexbulk.launch "$@"
