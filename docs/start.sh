#!/usr/bin/env sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
command -v node >/dev/null 2>&1 || { echo 'Install Node.js 22 or newer first.' >&2; exit 1; }
if [ ! -d node_modules ]; then
  npm ci
fi
exec npm start -- "$@"
