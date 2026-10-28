#!/bin/sh
set -eu

PORT="${PORT:-8000}"
python site_emulator.py --url "http://127.0.0.1:${PORT}" &
exec uvicorn server:app --host 0.0.0.0 --port "$PORT"
