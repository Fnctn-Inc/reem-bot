# Test Reem — quick guide

Hey team — would love your eyes (ears) on this. It's a real-time English voice agent that handles inbound car-insurance claim calls. Built solo for Big Berlin Hack 2026, Inca track.

**Should take you 2 minutes.**

## 1. Open the live dashboard first

Open this in a browser **before** you call:

→ **https://reem.fnctn.io/dashboard**

The dashboard shows every call as it arrives in the sidebar. When you call, your session pops in at the top in real time, and you'll see facts get extracted live as you talk to Reem.

## 2. Call this number

→ **+49 30 4243 1626**

(German DID, but Reem speaks English.)

She picks up after one ring with a short greeting and asks if anyone's hurt.

## 3. Try one of these scenarios

Pick whatever feels natural — Reem will adapt. Suggested scripts:

**Easy (parking lot bump, no injuries):**
> "Hi, I just backed into a pole at the Edeka parking lot on Friedrichstraße. My plate is B-MW-1234. No one's hurt but the rear bumper's dented."

**Medium (highway collision, mild injury):**
> "Someone rear-ended me on the A2 near Hannover. I'm okay but my passenger says her neck hurts. The other car's plate is HH-KL-998."

**Stress test (suspicious claim — fraud signal):**
> "I noticed a brand new scratch on my car this morning. Must've happened in the parking lot overnight. No one saw it. I want to file a claim."

## 4. What to look for on the dashboard

- **Sidebar** — your call appears at the top with a "● live" badge.
- **Conversation** — turn-by-turn transcript streams in.
- **Extracted facts** — caller info, plate, location, injuries, other party, etc. Cells flash gold when a value updates.
- **Fraud signal** — score 0–1 with a colour bar. The fraud-test scenario above should bump it.
- **Code-mode efficiency** — token-saving stats from our typed-sandbox tool approach (~97% smaller than naive function-calling).

## 5. What's special about it

- **Empathy first.** Reem asks about injuries before asking for any data — exactly what a real claims handler does.
- **Database-first.** As soon as you give a plate, she pulls policyholder/vehicle/coverage from the (mocked) policy DB and *stops* asking for things she already has.
- **Live judge dashboard.** No log-watching needed.
- **Edge-deployed.** Whole pipeline runs on Cloudflare Containers in the EU region. ~720 ms first-token latency.

## 6. If something seems off

- **No one picks up?** Another team on our shared Twilio account probably overwrote the number's voice config. There's a cron rebinding it every 5 min — try again in a sec.
- **Picks up but silent?** Gemini API credits might've drained again. Ping me.
- **Dashboard says "reconnecting…"?** Container goes to sleep after 15 min idle. The first call wakes it up; refresh once you're connected.

Source code: https://github.com/Fnctn-Inc/reem-bot

Thanks 🙏
