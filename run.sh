#!/usr/bin/env bash
# MAD Fitness — one-command local start: ./run.sh   (add --reset to wipe and re-seed the demo data)
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
  # keep Dropbox/iCloud from syncing thousands of venv files (harmless elsewhere)
  xattr -w com.dropbox.ignored 1 .venv 2>/dev/null || true
fi
.venv/bin/pip install -q -r requirements.txt
[ -f .env ] || cp .env.example .env

.venv/bin/python manage.py migrate --noinput -v0
if [ "${1:-}" = "--reset" ]; then
  .venv/bin/python manage.py seed --reset
else
  .venv/bin/python manage.py seed
fi

echo ""
echo "  Studio is running at http://localhost:8000"
echo "  Admin:   owner@studio.test / demo1234"
echo "  Trainer: alex@studio.test  / demo1234"
echo "  Client:  emily.carter@example.com / demo1234"
echo ""
exec .venv/bin/python manage.py runserver 8000
