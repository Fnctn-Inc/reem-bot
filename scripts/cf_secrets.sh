#!/usr/bin/env bash
# Push the secrets the Reem container needs into the Worker's secret store.
# Wrangler exposes them to the container as env vars at runtime.
#
# Run AFTER `wrangler deploy`. Reads keys from .env so we don't have to retype.
set -euo pipefail
cd "$(dirname "$0")/.."

export CLOUDFLARE_ACCOUNT_ID=34bb0b7abee7d135f8283c9bc69a32c0

# shellcheck disable=SC2046
set -a; source .env; set +a

# Override PUBLIC_HOST so the TwiML rendered inside the container points at
# the deployed CF route — not the laptop tunnel.
PUBLIC_HOST="reem.fnctn.io"

for key in \
    GRADIUM_API_KEY \
    GOOGLE_API_KEY \
    TAVILY_API_KEY \
    TWILIO_ACCOUNT_SID \
    TWILIO_API_KEY_SID \
    TWILIO_API_KEY_SECRET \
    TWILIO_PHONE_NUMBER \
    GRADIUM_VOICE_ID \
    PUBLIC_HOST
do
    val="${!key:-}"
    if [ -z "$val" ]; then
        echo "skip $key (empty)"
        continue
    fi
    printf '%s' "$val" | bunx wrangler secret put "$key" || echo "warn: $key failed"
done
