# Reem — FNOL Voice Agent

> Big Berlin Hack 2026 submission. A real-time English voice agent that handles inbound first-notice-of-loss calls for car insurance — empathic, low-latency, deployed on Cloudflare's edge, with a live judge-visible dashboard.

📞 **Live demo number:** _see submission form_
🌍 **Landing page:** https://reem.fnctn.io
📊 **Live dashboard:** https://reem.fnctn.io/dashboard

---

## What this is

Most voice-AI agents optimise for crispness and a corporate tone. Real humans on a fender-bender call are warm, slightly disfluent, and ask about injuries *before* they ask for data. Reem is engineered for that, and turns the call into a structured claim record live as it speaks.

What this project bets on:

1. **Empathy-first state machine + punctuation-driven prosody.** Reem's first turn is always an injury check. Pacing comes from natural punctuation (em-dashes, ellipses, periods) — the TTS voice's own prosody model handles the rhythm, mirroring how Gradium's own production demos work. Any stray SSML the LLM might still emit is normalised at the TTS boundary so it never gets read aloud.
2. **Database-first questioning.** The moment Reem has a plate or policy number, she queries the policy DB through code-mode and *stops asking for things she just received* (vehicle, policyholder, coverage, prior claims). This is the gold standard the Inca claims expert specifically called out in the channel.
3. **Code-mode for MCP.** Instead of one tool per backend (CRM, fraud, claim DB, photo, Tavily, dashboard), Gemini gets a single `execute_typescript` tool. It writes TypeScript that calls typed globals; we run it in a Bun sandbox. ~97 % smaller tool surface, parallel ops via `Promise.all`, and PII never enters the LLM's reasoning context.
4. **Live judge-visible dashboard.** Every extracted fact streams to a public web dashboard in real time. Sidebar lists every recent call (live + ended); the active call shows extracted facts (with flash-on-update), live transcript, fraud signal, and code-mode token-saving stats.
5. **Edge-deployed on Cloudflare Containers.** The whole pipeline (FastAPI + Pipecat + Bun sandbox) runs as a Container with a Durable Object backing, EU-region, behind a custom domain. No tunnels, no flaky DNS — Twilio Media Streams hits the edge directly.

## Architecture

```
PSTN call → Twilio (DID +49 30…)
          ↓ TwiML <Connect><Stream> (bound via voice_application_sid)
        wss://reem.fnctn.io/ws/twilio
          ↓
   ┌────────────────────────────────────────────────┐
   │  Cloudflare Worker (front door)                │
   │  + Cron Trigger every 5 min: re-bind Twilio    │
   │    number to our TwiML App (idempotent)        │
   └────────────────────┬───────────────────────────┘
                        ↓
   ┌────────────────────────────────────────────────┐
   │  Cloudflare Container  (Durable Object backed) │
   │  ┌──────────────────────────────────────────┐  │
   │  │ FastAPI (hud_relay.py)                   │  │
   │  │  • /                → landing page       │  │
   │  │  • /dashboard       → live dashboard     │  │
   │  │  • /ws/twilio       → Pipecat pipeline   │  │
   │  │  • /ws              → dashboard WebSocket│  │
   │  │  • /codemode/*      → typed mock backend │  │
   │  │  • /twiml/inbound   → returns Stream XML │  │
   │  └──────────────────────────────────────────┘  │
   │  ┌──────────────────────────────────────────┐  │
   │  │ Pipecat pipeline                         │  │
   │  │   Twilio μ-law 8 kHz                     │  │
   │  │     → Silero VAD                         │  │
   │  │     → Gradium STT (English)              │  │
   │  │     → Gemini 3.1 Flash Lite              │  │
   │  │     → Gradium TTS (voice "Eva")          │  │
   │  │     → Twilio                             │  │
   │  └──────────────────────────────────────────┘  │
   │  ┌──────────────────────────────────────────┐  │
   │  │ Bun sandbox (codemode/runner.py)         │  │
   │  │   executes Gemini-written TypeScript     │  │
   │  │   over typed mocks: crm / fraud /        │  │
   │  │   claimDb / tavily / photo / dashboard   │  │
   │  └──────────────────────────────────────────┘  │
   └────────────────────────────────────────────────┘
```

## Partner technologies

| # | Tech | Role |
|---|------|------|
| 1 | **Gradium** | English STT + TTS (voice "Eva" — `ubuXFxVQwVYnZQhy`) |
| 2 | **Google Gemini** | Reasoning + tool calling (`gemini-3.1-flash-lite-preview`) |
| 3 | **Tavily** | Optional research tool surfaced to Gemini via code-mode |
| 4 | **Cloudflare** | Containers + Worker (cron rebind) + custom domain edge deployment |
| 5 | **Twilio** | DID + Media Streams |

## Repo layout

```
src/
  agent.py        # Pipecat pipeline factory + DisfluencyTTS wrapper
                  # + FrameProbe that streams transcripts to the dashboard
  hud_relay.py    # FastAPI: landing, dashboard, /ws/twilio, /ws,
                  # /codemode/* (typed APIs), /twiml/inbound
  persona.py      # State machine + <break>-tag normaliser + disfluency
  codemode/
    runner.py     # Python → Bun sandbox bridge
    sandbox.ts    # Globals: crm, fraud, claimDb, tavily, photo, dashboard
    typings.d.ts  # TypeScript surface the LLM writes against
  diag.py         # In-process diagnostic ring buffer
  aiq.py          # Audio Intelligence Quotient tracker
worker/
  index.ts        # Cloudflare Worker + ReemContainer Durable Object
                  # + scheduled() handler for the Twilio rebind cron
wrangler.jsonc    # CF deployment config (custom domain + cron)
Dockerfile        # Container image (Python 3.11 + Bun + uv, prewarms Silero)
scripts/
  point_twilio_to_cf.py  # Bind Twilio number to the TwiML App
  cf_secrets.sh          # Bulk-push secrets from .env to Cloudflare
tests/
  test_persona.py        # State machine + tag normaliser tests
SUBMISSION_MANUAL.md     # How to test (number, dashboard, scenarios)
```

## Setup — local development

Requires: Python 3.11+, [uv](https://docs.astral.sh/uv/), [bun](https://bun.sh/), and any HTTPS tunnel that can forward WebSockets ([ngrok](https://ngrok.com/), Cloudflare quick tunnel, etc.).

```bash
# 1. Install deps
uv sync
bun install

# 2. Configure env
cp .env.example .env
# Fill in TWILIO_*, GRADIUM_API_KEY, GOOGLE_API_KEY, TAVILY_API_KEY, PUBLIC_HOST

# 3. Run the FastAPI relay (Pipecat agent + dashboard + code-mode endpoints)
uv run python -m src.hud_relay

# 4. Expose it publicly so Twilio can reach it
ngrok http 8765   # set the resulting hostname as PUBLIC_HOST in .env

# 5. Point your Twilio number at it
uv run python scripts/point_twilio_to_cf.py
```

Then call your `TWILIO_PHONE_NUMBER` — Reem picks up. Open `http://localhost:8765/dashboard` while you call to watch facts and transcripts stream in.

## Setup — production (Cloudflare Containers)

Requires: a Cloudflare account, [wrangler](https://developers.cloudflare.com/workers/wrangler/install-and-update/), and Docker (for the local image build).

```bash
# 1. Authenticate
npx wrangler login

# 2. Push secrets (never commit these). Either run cf_secrets.sh which
#    sources from .env, or push them one at a time:
npx wrangler secret put GRADIUM_API_KEY
npx wrangler secret put GRADIUM_VOICE_ID
npx wrangler secret put GOOGLE_API_KEY
npx wrangler secret put TAVILY_API_KEY
npx wrangler secret put TWILIO_ACCOUNT_SID
npx wrangler secret put TWILIO_API_KEY_SID
npx wrangler secret put TWILIO_API_KEY_SECRET
npx wrangler secret put TWILIO_PHONE_NUMBER
npx wrangler secret put TWILIO_TWIML_APP_SID
npx wrangler secret put PUBLIC_HOST

# 3. Deploy
npx wrangler deploy
```

Cloudflare builds the Dockerfile, pushes the image to its registry, attaches the Container to a Durable Object class, wires the custom domain, and registers the `*/5 * * * *` cron that re-binds the Twilio number to the TwiML App.

After the first deploy, point Twilio at the production endpoint:

```bash
PUBLIC_HOST=reem.fnctn.io uv run python scripts/point_twilio_to_cf.py
```

The script binds the number to a TwiML Application (`voice_application_sid`) rather than a `voice_url`. That binding survives the common shared-Twilio-account failure mode where another developer overwrites the `voice_url` field, and the cron in the Worker re-applies it every 5 minutes if it ever gets cleared.

## Voice tuning — the hard-won knobs

The values currently shipping (see `src/agent.py`) come from live A/B testing on real calls plus a deep audit of Gradium's own [gradbot](https://github.com/gradium-ai/gradbot) production demos:

- **Voice = Eva** (`ubuXFxVQwVYnZQhy`) — *"joyful and dynamic British adult voice ideal for lively conversations"*. Used by name in Gradium's own `demos/business_bank/main.py`. Earlier picks ("Samantha", "Anna") were tagged "warm/managerial/airy" — descriptors that signal slow or telephony-fragile cadence.
- **`json_config` at Gradium's documented defaults**: `padding_bonus -0.5` (negative = faster, per `restaurant_ordering` demo), `temp 0.7` (default), `cfg_coef 2.0` (default), `rewrite_rules "en"`. An earlier attempt at sub-default `temp 0.55` / `cfg_coef 1.6` flattened prosody and made the voice feel robotic — those values *below* defaults collapse prosodic variance.
- **Punctuation-driven prosody, no SSML.** Gradium's own demos contain zero `<break>` tags; their guidance is to control pacing through commas, periods, em-dashes, and ellipses, letting the voice's prosody model do the rest. The system prompt now forbids tags entirely, and `persona.strip_break_tags()` defensively removes any the model might still leak — one earlier hot-seat issue was Gradium reading the tag aloud as text.
- **Greeting is LLM-generated**, not canned. A hand-crafted opener never matches the prosody variance of the rest of the call. The context is seeded with a minimal `[Call just connected]` primer, and the greeting-only rules (one sentence, no tools, max 12 words, no leading pause) live in the system prompt's `FIRST RESPONSE` section, scoped explicitly so they don't bleed into mid-call turns.
- **Self-quote stripping at the TTS boundary.** Gemini occasionally wraps its response in quote characters, which Gradium then reads in a *different voice*. `persona.strip_self_quoting()` strips outer quote characters before TTS.
- **Gemini 3.1 Flash Lite** beats 2.5 Flash on first-token latency (~720 ms vs ~2 s) for short conversational turns. Lite has no chain-of-thought, so no `thinking_budget` needed.
- **Dashboard session-open is fire-and-forget.** Awaiting an HTTP round-trip in `on_client_connected` was adding ~300 ms before the greeting started speaking. Now the LLM frame queues *first*, dashboard registration runs in parallel.

## Live dashboard

The dashboard at `/dashboard` is a single-page light-themed UI that shows:

- **Sidebar** — every call as it arrives (newest at the top, capped at 20). Each entry shows the call's short id, status (live / ended), the most recent turn preview, and how long ago it started.
- **Detail pane** — for the selected call:
  - **Conversation** — turn-by-turn transcript (user + assistant), auto-scrolls.
  - **Extracted facts** — caller, role, plate, policy, vehicle, location, injuries, other party, police, witnesses, photos, claim id, stage. Cells flash gold when a value changes.
  - **Fraud signal** — 0–1 score with colour-coded bar plus a list of flag badges.
  - **Code-mode efficiency** — naive vs code-mode tokens, % saved, last result.

Updates flow over a single `/ws` WebSocket; the page hydrates from `/codemode/dashboard/state` on load and reconnects automatically.

The agent keeps the dashboard in sync via three mechanisms:

1. `dashboard.update(facts)` — typed API the LLM calls inside `execute_typescript` whenever it extracts something new.
2. Pipeline `FrameProbe` instances — broadcast user transcriptions on `TranscriptionFrame` and assistant text on `LLMFullResponseEndFrame`.
3. `on_client_connected` / `on_client_disconnected` event handlers — open and close the dashboard session keyed by the Twilio call SID.

## Tests

```bash
uv run pytest -q
```

Covers the persona state machine, the `<break>`-tag normaliser (canonical forms, single-quote rewriting, missing-space repair, unparseable-tag stripping, duration clamping), and the AIQ tracker.
