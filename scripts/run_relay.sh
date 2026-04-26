#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run uvicorn src.hud_relay:app --host 0.0.0.0 --port "${HUD_WS_PORT:-8765}" --reload
