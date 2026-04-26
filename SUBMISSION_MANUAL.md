# Reem — Submission Manual

> Big Berlin Hack 2026 · Inca track · FNCTN.io

A real-time voice agent that handles inbound first-notice-of-loss (FNOL) car-insurance calls. Empathy-first, sub-second turn-taking, deployed on the Cloudflare edge.

---

## How to test it

### 1. Call this number

**☎️ +49 30 4243 1626**

(Direct German DID, routed via Twilio Media Streams to a Cloudflare Container running the Pipecat pipeline.)

Reem picks up after one ring. She greets you, asks if anyone is hurt, and goes from there.

### 2. Watch the live dashboard

🌍 **https://reem.fnctn.io/dashboard**

Every fact Reem extracts from the call is pushed to the dashboard live. Open it in a browser before you call so you can watch the call become a structured claim record in real time.

What you'll see:
- **Extracted facts** — caller name, role, plate, location, injuries, other party, police involvement, etc. Updates as Reem learns each one.
- **Fraud signal** — live score 0–1 with a colour-coded bar and any flags Reem has raised.
- **Code-mode stats** — how many tokens the typed-sandbox approach saved versus naive function-calling on this turn.
- **Raw stream** — the WebSocket event log if you want to inspect message-by-message.

The landing page at **https://reem.fnctn.io/** has the same dashboard linked from a "Open the live dashboard" button.

### 3. Sample call scenarios

Pick whichever feels natural — Reem will adapt. Suggested scripts:

#### Scenario A — Minor parking-lot bump (no injuries)

> "Hi, I just backed into a pole at the Edeka parking lot on Friedrichstraße. My plate is B-MW-1234. No one's hurt but the rear bumper's dented."

What Reem should do:
- Express brief sympathy
- Confirm no injuries
- Look up the plate via the database (you should see policy details appear on the dashboard)
- Ask for a brief description and whether the car's drivable
- Wrap up with "you'll get a confirmation by email"

#### Scenario B — Highway collision with another driver (caller mildly shaken)

> "Someone rear-ended me on the A2 near Hannover. I'm okay but my passenger says her neck hurts. The other car's plate is HH-KL-998."

What Reem should do:
- Lead with empathy ("oh no", "are you okay")
- Ask about injuries first, capture passenger
- Get location, other-party plate, ask if police are on scene
- Look up your policy, ask whether the car's drivable
- Walk you through next steps

#### Scenario C — Suspicious claim (fraud signal exercise)

> "Yeah, I noticed a brand new scratch on my car this morning. Must've happened in the parking lot overnight. No one saw anything. I'd like to file a claim."

What Reem should do:
- Empathic acknowledgment
- Collect the basics (plate, location, "no other party")
- Quietly score for fraud — vague "no witnesses, fresh damage, no other party" is a known signal pattern
- Surface the score on the dashboard (mid-range)
- Still complete the intake politely

### 4. Languages supported

- **English** (primary). Reem speaks American English (Gradium voice "Samantha"), recognises any English accent.
- The system prompt also instructs Reem to **switch language if the caller does**. STT is currently locked to English, so heavy accent / mixed-language input may degrade gracefully but not flawlessly.

### 5. Stack used

| Layer | Technology |
|---|---|
| Phone | Twilio (German DID, Media Streams over WebSocket) |
| Real-time pipeline | Pipecat 1.0 |
| Voice activity | Silero VAD (ONNX, in-process) |
| STT | Gradium (`Language.EN`) |
| LLM | Google Gemini 3.1 Flash Lite Preview (~720 ms first-token) |
| TTS | Gradium voice "Samantha" (`mn5sS7D8kYKETZXA`) |
| Tooling | Code-mode for MCP — single `execute_typescript` tool over a typed Bun sandbox |
| Edge | Cloudflare Container Durable Object behind a custom domain |

### 6. What makes this submission different

1. **Empathy-first** — the system prompt forces Reem to ask about injuries before any fact-gathering.
2. **Live LLM-driven `<break>`-tag prosody** — pause placement comes from Gemini's full semantic context, normalised at the TTS boundary so Gradium renders the tags as real silence (not spoken).
3. **Database-first questioning** — the moment Reem has a plate or policy number, she queries the policy DB and *stops asking for things she just received*. This is exactly what Flo (Inca) called out as the gold standard in the Discord.
4. **Code-mode for MCP** — instead of a tool surface that grows linearly with each backend, Reem has one `execute_typescript` tool over a typed sandbox (`crm`, `fraud`, `claimDb`, `tavily`, `photo`, `dashboard`). ~97% smaller tool surface, parallel ops via `Promise.all`, PII never enters the LLM's reasoning context.
5. **Live judge-visible dashboard** — every extraction streams to a public web view in real time, so testing the agent doesn't require staring at logs.
6. **Resilient to the shared-Twilio-account problem** — the number is bound via `voice_application_sid` (TwiML App), not `voice_url`, so config edits by other teams don't break us. A scheduled Cloudflare Worker re-binds every 5 minutes as a belt-and-braces measure.

### 7. What to look for while judging

- Reem opens with empathy, not data collection.
- Pauses sound natural, not metronome-y.
- After you say a plate, the dashboard fills in policyholder/vehicle/coverage *before* Reem asks for those things.
- The fraud score moves when scenario C-style signals show up.
- Token-savings card on the dashboard updates after each tool call, demonstrating the code-mode efficiency claim with live numbers.

### 8. Source code

Public repo: **https://github.com/Fnctn-Inc/reem-bot**

### 9. If something seems off

- **Reem doesn't pick up at all** → another team on the shared Twilio account probably overwrote the number's voice config. The cron rebinds every 5 minutes; try again.
- **Reem picks up but is silent** → the Google Gemini API project credits are depleted. Inca explicitly offered key top-ups in the Discord — that's the unblock.
- **Dashboard says "reconnecting…"** → the container goes to sleep after 15 minutes idle. The first call wakes it; refresh the dashboard once you're connected.

---

Made with love by humans and agents at [FNCTN.io](https://fnctn.io).
