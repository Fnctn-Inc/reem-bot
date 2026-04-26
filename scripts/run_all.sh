#!/usr/bin/env bash
# Run the HUD relay + voice agent in parallel. Both write logs to /tmp.
# Press Ctrl-C to stop both.
set -euo pipefail
cd "$(dirname "$0")/.."

# Kill any leftover from a previous run
pkill -f 'uvicorn src.hud_relay' 2>/dev/null || true
pkill -f 'src.agent' 2>/dev/null || true
sleep 1

echo "→ starting HUD relay on :${HUD_WS_PORT:-8765}"
uv run uvicorn src.hud_relay:app --host 0.0.0.0 --port "${HUD_WS_PORT:-8765}" \
    > /tmp/lena-relay.log 2>&1 &
RELAY_PID=$!

sleep 2

echo "→ starting Lena voice agent"
uv run python -m src.agent dev > /tmp/lena-agent.log 2>&1 &
AGENT_PID=$!

trap 'echo "stopping"; kill $RELAY_PID $AGENT_PID 2>/dev/null; exit 0' INT TERM

echo
echo "ready. tail with:"
echo "  tail -f /tmp/lena-agent.log"
echo "  tail -f /tmp/lena-relay.log"
echo
echo "DIAL:  +493042431626"
echo
echo "(Ctrl-C to stop)"

wait
