#!/bin/sh
set -eu

exec python -m uvicorn app:app --host 0.0.0.0 --port "${PORT:-3000}" --proxy-headers
