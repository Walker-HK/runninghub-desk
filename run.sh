#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
        exec "$candidate" app.py "$@"
    fi
done
printf '%s\n' 'Python 3.10+ is required. Install Python and add it to PATH.' >&2
exit 1
