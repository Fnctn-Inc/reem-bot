"""FastAPI server: Twilio Media Streams ↔ Pipecat agent + HUD relay + code-mode gateway.

Three surfaces in one process:

1. /ws/twilio  — Twilio Media Streams WebSocket. Pipecat agent handles
                 audio bidirectionally. The TwiML at /twiml/inbound points
                 Twilio here.

2. /ws         — Lovable HUD WebSocket (broadcasts AIQ + code-mode stats).

3. /codemode/* — Typed mock backend the Bun sandbox calls when running
                 model-written TypeScript (mirrors codemode/typings.d.ts).
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel


load_dotenv()


from src.diag import diag, snapshot as diag_snapshot

app = FastAPI(title="Reem Voice Agent + HUD Relay")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _prewarm() -> None:
    """Eager-load Pipecat + Silero + Smart Turn + agent module at boot.

    Without this, the first inbound call pays ~2s of import + ONNX-load
    latency before Reem can even start generating audio. Pre-warming makes
    the cold call indistinguishable from steady-state.
    """
    import time as _t

    t0 = _t.monotonic()
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    SileroVADAnalyzer()  # load ONNX into memory
    import src.agent  # noqa: F401 — also pulls in google/gradium imports
    logger.info(f"prewarm complete in {_t.monotonic()-t0:.2f}s")


# --------- HUD WebSocket ---------

_clients: list[WebSocket] = []
_lock = asyncio.Lock()


@app.websocket("/ws")
async def ws_endpoint(socket: WebSocket) -> None:
    await socket.accept()
    async with _lock:
        _clients.append(socket)
    try:
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        async with _lock:
            if socket in _clients:
                _clients.remove(socket)


async def broadcast(msg: dict) -> None:
    payload = json.dumps(msg)
    async with _lock:
        targets = list(_clients)
    for c in targets:
        try:
            await c.send_text(payload)
        except Exception:
            pass


# --------- Twilio Media Streams WebSocket → Pipecat ---------


@app.websocket("/ws/twilio")
async def twilio_ws(socket: WebSocket) -> None:
    """Inbound Twilio Media Stream. Hands the socket to the Pipecat bot."""
    await socket.accept()
    diag("ws_accepted", client=str(socket.client))
    logger.info(f"twilio websocket accepted from client={socket.client}")
    from src.agent import run_bot

    try:
        await run_bot(socket)
    except WebSocketDisconnect:
        diag("ws_disconnect", reason="clean")
        logger.info("twilio websocket disconnected cleanly")
    except Exception as e:
        diag("ws_crash", error=str(e), error_type=type(e).__name__)
        logger.exception(f"twilio bot crashed: {e}")
    finally:
        diag("ws_handler_exit")
        logger.info("twilio websocket handler exiting")


# --------- Code-mode gateway endpoints ---------
# Typed APIs the Bun sandbox (sandbox.ts) calls. Mirrors typings.d.ts.


class PlateLookup(BaseModel):
    plate: str


@app.post("/codemode/crm/lookup")
def crm_lookup(b: PlateLookup) -> dict[str, Any]:
    if not b.plate:
        return None  # type: ignore[return-value]
    return {
        "owner": "Anna Schmidt",
        "vehicle": "VW Golf VII (2019)",
        "policy_id": "POL-883422",
    }


class FraudCheckBody(BaseModel):
    plate: str
    description: str
    location: str


@app.post("/codemode/fraud/check")
def fraud_check(b: FraudCheckBody) -> dict[str, Any]:
    flags: list[str] = []
    desc = b.description.lower()
    if "neu" in desc and ("kratzer" in desc or "lack" in desc):
        flags.append("possibly-staged-cosmetic-only")
    if "nacht" in desc and "parkplatz" in desc:
        flags.append("nighttime-parking-claim")
    return {"score": 0.05 + 0.2 * len(flags), "flags": flags}


class ClaimWriteBody(BaseModel):
    data: dict[str, Any]


@app.post("/codemode/claim_db/write")
def claim_write(b: ClaimWriteBody) -> dict[str, Any]:
    import hashlib

    raw = json.dumps(b.data, sort_keys=True, ensure_ascii=False).encode()
    h = hashlib.blake2b(raw, digest_size=4).hexdigest().upper()
    return {"claim_id": f"CLM-{h}"}


class TavilyResearchBody(BaseModel):
    query: str


@app.post("/codemode/tavily/research")
def tavily_research(b: TavilyResearchBody) -> dict[str, Any]:
    from src.tavily_tool import lookup_claim_context

    return lookup_claim_context(b.query)


class PhotoDescribeBody(BaseModel):
    url: str


@app.post("/codemode/photo/describe")
def photo_describe(b: PhotoDescribeBody) -> dict[str, Any]:
    return {
        "description": "Rear bumper lightly dented, paint scuff on right side",
        "damage_severity": "low",
    }


# In-memory session store. Each call is a session keyed by Twilio call_id (or
# a synthetic id for local dev). Newest first; capped at 20 so the dashboard
# stays snappy. The Cloudflare Container restarts wipe this — fine for a demo
# (judges open the dashboard, then call, then watch their session land first).
import time as _time
import uuid as _uuid

_sessions_lock = asyncio.Lock()
_sessions: dict[str, dict[str, Any]] = {}  # id → session
_session_order: list[str] = []  # newest first
_current_session_id: str | None = None
_MAX_SESSIONS = 20


def _new_session(call_id: str | None = None) -> dict[str, Any]:
    sid = call_id or f"local-{_uuid.uuid4().hex[:8]}"
    return {
        "id": sid,
        "short_id": sid[-6:],
        "started_at": _time.time(),
        "ended_at": None,
        "status": "live",  # live | ended
        "facts": {},
        "transcript": [],  # list of {role, text, t}
        "fraud_score": None,
        "fraud_flags": [],
    }


async def _start_session(call_id: str | None) -> dict[str, Any]:
    global _current_session_id
    async with _sessions_lock:
        s = _new_session(call_id)
        _sessions[s["id"]] = s
        _session_order.insert(0, s["id"])
        # Cap
        while len(_session_order) > _MAX_SESSIONS:
            old = _session_order.pop()
            _sessions.pop(old, None)
        _current_session_id = s["id"]
        snapshot = dict(s)
    await broadcast({"type": "session_started", "session": snapshot})
    return snapshot


async def _end_session(call_id: str | None) -> None:
    async with _sessions_lock:
        sid = call_id if call_id and call_id in _sessions else _current_session_id
        if not sid or sid not in _sessions:
            return
        _sessions[sid]["status"] = "ended"
        _sessions[sid]["ended_at"] = _time.time()
        snapshot = dict(_sessions[sid])
    await broadcast({"type": "session_ended", "session": snapshot})


async def _patch_session(facts: dict[str, Any]) -> dict[str, Any] | None:
    async with _sessions_lock:
        sid = _current_session_id
        if not sid or sid not in _sessions:
            # Auto-create a session if facts arrive without an explicit start
            s = _new_session(None)
            _sessions[s["id"]] = s
            _session_order.insert(0, s["id"])
            _current_session_id = s["id"]
            sid = s["id"]
        s = _sessions[sid]
        s["facts"].update(facts)
        if "fraud_score" in facts:
            s["fraud_score"] = facts["fraud_score"]
        if "fraud_flags" in facts:
            s["fraud_flags"] = facts["fraud_flags"]
        snapshot = dict(s)
    await broadcast({"type": "session_facts", "id": sid, "facts": snapshot["facts"]})
    return snapshot


async def _append_transcript(role: str, text: str) -> None:
    async with _sessions_lock:
        sid = _current_session_id
        if not sid or sid not in _sessions:
            return
        item = {"role": role, "text": text, "t": _time.time()}
        _sessions[sid]["transcript"].append(item)
    await broadcast({"type": "session_transcript", "id": sid, "item": item})


class DashboardUpdateBody(BaseModel):
    facts: dict[str, Any]


@app.post("/codemode/dashboard/update")
async def dashboard_update(b: DashboardUpdateBody) -> dict[str, bool]:
    """LLM-driven live dashboard updates. Merge into the current session."""
    await _patch_session(b.facts)
    return {"ok": True}


class SessionLifecycleBody(BaseModel):
    call_id: str | None = None


@app.post("/codemode/dashboard/start")
async def dashboard_start(b: SessionLifecycleBody) -> dict[str, str]:
    s = await _start_session(b.call_id)
    return {"id": s["id"]}


@app.post("/codemode/dashboard/end")
async def dashboard_end(b: SessionLifecycleBody) -> dict[str, bool]:
    await _end_session(b.call_id)
    return {"ok": True}


@app.post("/codemode/dashboard/reset")
async def dashboard_reset() -> dict[str, bool]:
    """Compatibility shim — older code calls this on connect."""
    await _start_session(None)
    return {"ok": True}


class TranscriptBody(BaseModel):
    role: str  # "user" | "assistant"
    text: str


@app.post("/codemode/dashboard/transcript")
async def dashboard_transcript(b: TranscriptBody) -> dict[str, bool]:
    await _append_transcript(b.role, b.text)
    return {"ok": True}


@app.get("/codemode/dashboard/state")
async def dashboard_state() -> dict[str, Any]:
    async with _sessions_lock:
        return {
            "sessions": [_sessions[i] for i in _session_order if i in _sessions],
            "current_id": _current_session_id,
        }


# --------- Internal broadcast (agent → HUD) ---------


class BroadcastBody(BaseModel):
    pass

    model_config = {"extra": "allow"}


@app.post("/internal/broadcast")
async def internal_broadcast(b: BroadcastBody) -> dict[str, str]:
    msg = b.model_dump()
    await broadcast(msg)
    return {"sent": "ok"}


# --------- TwiML for inbound Twilio calls ---------


_PHONE_DISPLAY = os.getenv("TWILIO_PHONE_NUMBER", "+49 30 4243 1626")


_LANDING_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reem · Voice agent for insurance claims</title>
<style>
  :root {
    --bg: #0c0d10;
    --surface: #14161b;
    --border: #23262d;
    --fg: #f3f1eb;
    --muted: #98a0a8;
    --primary: #ffd28b;
    --accent: #c8a65d;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--fg); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
  a { color: inherit; }
  .wrap { max-width: 1100px; margin: 0 auto; padding: 80px 24px 48px; }
  .pill { display: inline-block; border: 1px solid var(--border); border-radius: 999px; padding: 6px 14px; font-size: 13px; color: var(--muted); background: var(--surface); }
  h1 { font-size: clamp(2.25rem, 5vw, 4.5rem); line-height: 1.05; margin: 24px 0 0; font-weight: 600; max-width: 18ch; letter-spacing: -0.02em; }
  .lede { color: var(--muted); font-size: clamp(1rem, 1.4vw, 1.25rem); line-height: 1.6; max-width: 60ch; margin-top: 24px; }
  .cta { display: inline-flex; gap: 10px; align-items: center; margin-top: 36px; padding: 14px 22px; border-radius: 12px; background: var(--primary); color: #1a1408; font-weight: 600; text-decoration: none; transition: transform 80ms ease; }
  .cta:hover { transform: translateY(-1px); }
  .cta-secondary { background: transparent; color: var(--fg); border: 1px solid var(--border); margin-left: 10px; }
  .grid { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin-top: 64px; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 14px; padding: 22px; }
  .stat { font-size: 1.5rem; font-weight: 600; color: var(--fg); }
  .stat-label { color: var(--muted); font-size: 13px; margin-top: 4px; }
  section { margin-top: 96px; }
  section h2 { font-size: clamp(1.5rem, 2.4vw, 2.25rem); margin: 0 0 12px; font-weight: 600; letter-spacing: -0.01em; }
  section p { color: var(--muted); line-height: 1.6; max-width: 70ch; }
  .features { display: grid; gap: 16px; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); margin-top: 32px; }
  .feature h3 { margin: 0 0 8px; font-size: 1.1rem; font-weight: 600; }
  .feature p { font-size: 14px; }
  .stack { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 24px; }
  .stack span { background: var(--surface); border: 1px solid var(--border); border-radius: 999px; padding: 6px 14px; font-size: 13px; color: var(--muted); }
  footer { margin-top: 96px; padding-top: 32px; border-top: 1px solid var(--border); color: var(--muted); font-size: 13px; display: flex; justify-content: space-between; flex-wrap: wrap; gap: 12px; }
  footer a { color: var(--fg); text-decoration: none; border-bottom: 1px dotted var(--muted); }
  code { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: 2px 8px; font-size: 0.9em; color: var(--primary); }
</style>
</head>
<body>
<main class="wrap">
  <span class="pill">Built for Big Berlin Hack 2026 · Inca track</span>
  <h1>A voice agent that handles claims like a calm human, not a script.</h1>
  <p class="lede">Reem answers your insurance line, asks about injuries first, looks up the policy live so she never asks twice, and turns the call into structured facts before the caller hangs up. Real-time English, sub-second turn-taking, deployed at Cloudflare's edge.</p>

  <a class="cta" href="tel:__PHONE__">Call Reem · __PHONE__</a>
  <a class="cta cta-secondary" href="/dashboard">Open the live dashboard →</a>

  <div class="grid">
    <div class="card"><div class="stat">~720 ms</div><div class="stat-label">first-token latency on Gemini Flash Lite</div></div>
    <div class="card"><div class="stat">~97 %</div><div class="stat-label">tool-surface tokens saved by code-mode</div></div>
    <div class="card"><div class="stat">EU edge</div><div class="stat-label">deployed on Cloudflare Containers</div></div>
    <div class="card"><div class="stat">Live</div><div class="stat-label">judge-visible extraction dashboard</div></div>
  </div>

  <section>
    <h2>What's different</h2>
    <p>Most voice-AI agents optimise for crispness and a corporate tone. Real humans on a fender-bender call are warm, slightly disfluent, and ask about injuries before data. Reem is engineered for that — and runs the whole pipeline at the edge for sub-second latency.</p>
    <div class="features">
      <div class="feature card">
        <h3>Empathy-first state machine</h3>
        <p>Always asks if anyone's hurt before it asks for the plate. LLM-driven SSML pause tags give human breath rhythm; never reads markup aloud.</p>
      </div>
      <div class="feature card">
        <h3>Code-mode for MCP</h3>
        <p>Instead of one tool per backend, Gemini gets a single <code>execute_typescript</code> tool over a typed Bun sandbox. ~97% smaller tool surface, parallel ops via <code>Promise.all</code>.</p>
      </div>
      <div class="feature card">
        <h3>Database-first questioning</h3>
        <p>Reem queries the policy DB the moment she has a plate, then never asks for the policy holder, vehicle, or coverage scope she just received. Less friction for the caller.</p>
      </div>
      <div class="feature card">
        <h3>Live judge-visible dashboard</h3>
        <p>Every extracted fact streams into a public dashboard so anyone can watch the call become a structured claim record in real time.</p>
      </div>
    </div>
  </section>

  <section>
    <h2>Stack</h2>
    <div class="stack">
      <span>Pipecat 1.0</span>
      <span>Twilio Media Streams</span>
      <span>Gradium STT + TTS</span>
      <span>Google Gemini Flash Lite</span>
      <span>Tavily Research</span>
      <span>Cloudflare Containers</span>
      <span>Bun sandbox</span>
      <span>FastAPI</span>
    </div>
  </section>

  <footer>
    <span>Made with love by humans and agents at <a href="https://fnctn.io">FNCTN.io</a></span>
    <span><a href="/dashboard">Live dashboard</a></span>
  </footer>
</main>
</body>
</html>"""


_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Reem · Live call dashboard</title>
<style>
  :root {
    --bg: #fbfaf6;
    --surface: #ffffff;
    --border: #e9e3d4;
    --fg: #1a1a1a;
    --muted: #6b6b6b;
    --primary: #8a6a2c;
    --primary-soft: #fff3dd;
    --good: #2c8a5a;
    --warn: #b8761c;
    --bad: #c14444;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--fg); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, system-ui, sans-serif; -webkit-font-smoothing: antialiased; }
  a { color: var(--primary); text-decoration: none; }
  a:hover { text-decoration: underline; }
  header { padding: 18px 28px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; gap: 16px; background: var(--surface); position: sticky; top: 0; z-index: 10; }
  header h1 { margin: 0; font-size: 1rem; font-weight: 600; letter-spacing: -0.01em; }
  header h1 a { color: var(--fg); }
  .status { display: inline-flex; align-items: center; gap: 8px; padding: 5px 12px; border: 1px solid var(--border); border-radius: 999px; font-size: 12px; color: var(--muted); background: var(--bg); }
  .dot { width: 7px; height: 7px; border-radius: 50%; background: #ccc; }
  .dot.live { background: var(--good); box-shadow: 0 0 0 3px rgba(44,138,90,0.18); animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { box-shadow: 0 0 0 3px rgba(44,138,90,0.18); } 50% { box-shadow: 0 0 0 6px rgba(44,138,90,0.05); } }

  main { display: grid; grid-template-columns: 320px 1fr; min-height: calc(100vh - 60px); }
  @media (max-width: 920px) { main { grid-template-columns: 1fr; } }

  .sessions { border-right: 1px solid var(--border); background: var(--surface); overflow-y: auto; max-height: calc(100vh - 60px); }
  .sessions-head { padding: 16px 20px 8px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.08em; color: var(--muted); display: flex; justify-content: space-between; align-items: center; }
  .sessions-list { padding: 0 8px 16px; }
  .session { padding: 12px 14px; margin: 2px 0; border-radius: 10px; cursor: pointer; border: 1px solid transparent; transition: background 120ms; }
  .session:hover { background: var(--primary-soft); }
  .session.active { background: var(--primary-soft); border-color: var(--primary); }
  .session .row { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .session .id { font-family: ui-monospace, "SF Mono", monospace; font-size: 12px; color: var(--muted); }
  .session .when { font-size: 11px; color: var(--muted); }
  .session .preview { font-size: 13px; margin-top: 6px; color: var(--fg); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .session .live-tag { font-size: 10px; font-weight: 700; color: var(--good); text-transform: uppercase; letter-spacing: 0.08em; }
  .session .ended-tag { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.08em; }
  .empty-list { padding: 32px 20px; color: var(--muted); font-size: 13px; text-align: center; }

  .detail { padding: 24px; max-width: 1100px; }
  .detail h2 { margin: 0 0 4px; font-size: 1.5rem; font-weight: 600; letter-spacing: -0.01em; }
  .detail .sub { color: var(--muted); font-size: 13px; margin-bottom: 24px; }

  .grid-two { display: grid; gap: 16px; grid-template-columns: 1.4fr 1fr; }
  @media (max-width: 1100px) { .grid-two { grid-template-columns: 1fr; } }

  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px; }
  .card h3 { margin: 0 0 14px; font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--muted); letter-spacing: 0.08em; }

  .facts { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
  .fact { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; transition: background 200ms ease; }
  .fact.flash { background: var(--primary-soft); }
  .fact .k { color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 4px; }
  .fact .v { font-size: 14px; word-break: break-word; line-height: 1.4; }
  .fact .v.empty { color: var(--muted); font-style: italic; }

  .fraud { display: flex; align-items: center; gap: 12px; }
  .fraud-bar { flex: 1; height: 6px; background: var(--bg); border-radius: 999px; overflow: hidden; border: 1px solid var(--border); }
  .fraud-bar-fill { height: 100%; background: var(--good); transition: width 400ms ease, background 400ms ease; }
  .fraud-num { font-family: ui-monospace, monospace; font-size: 14px; min-width: 40px; text-align: right; }
  .badges { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
  .badge { background: var(--primary-soft); border: 1px solid var(--primary); border-radius: 5px; padding: 2px 7px; font-size: 11px; color: var(--primary); }

  .transcript { display: flex; flex-direction: column; gap: 8px; max-height: 420px; overflow-y: auto; }
  .turn { display: flex; gap: 10px; align-items: flex-start; }
  .turn .role { flex-shrink: 0; width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; color: white; }
  .turn.user .role { background: #6b6b6b; }
  .turn.assistant .role { background: var(--primary); }
  .turn .body { flex: 1; padding: 8px 12px; border-radius: 10px; background: var(--bg); border: 1px solid var(--border); font-size: 14px; line-height: 1.45; }
  .turn.assistant .body { background: var(--primary-soft); border-color: var(--primary); }
  .empty-msg { color: var(--muted); font-style: italic; font-size: 13px; padding: 12px 0; }

  footer { padding: 18px 28px; color: var(--muted); font-size: 12px; border-top: 1px solid var(--border); text-align: center; background: var(--surface); }
</style>
</head>
<body>
<header>
  <h1><a href="/">Reem</a> · live call dashboard</h1>
  <span class="status"><span class="dot" id="conn-dot"></span><span id="conn-label">connecting…</span></span>
</header>
<main>
  <aside class="sessions">
    <div class="sessions-head">
      <span>Calls</span>
      <span id="sessions-count" style="background:var(--bg);border:1px solid var(--border);border-radius:999px;padding:1px 8px;color:var(--muted);font-size:11px;">0</span>
    </div>
    <div class="sessions-list" id="sessions-list">
      <div class="empty-list">No calls yet.<br>Dial the number on <a href="/">the landing page</a> to see one appear here.</div>
    </div>
  </aside>
  <section class="detail">
    <h2 id="detail-title">Waiting for the next call…</h2>
    <p class="sub" id="detail-sub">When a call connects, extracted facts and the live transcript will appear here.</p>
    <div class="grid-two">
      <div>
        <div class="card">
          <h3>Conversation</h3>
          <div class="transcript" id="transcript">
            <div class="empty-msg">Live transcript will appear here turn by turn.</div>
          </div>
        </div>
        <div class="card" style="margin-top:16px;">
          <h3>Fraud signal</h3>
          <div class="fraud">
            <div class="fraud-bar"><div class="fraud-bar-fill" id="fraud-fill" style="width:0%;"></div></div>
            <span class="fraud-num" id="fraud-score">—</span>
          </div>
          <div class="badges" id="fraud-flags"></div>
        </div>
      </div>
      <div>
        <div class="card">
          <h3>Extracted facts</h3>
          <div class="facts" id="facts"></div>
        </div>
        <div class="card" style="margin-top:16px;">
          <h3>Code-mode efficiency</h3>
          <div class="facts" id="codemode"></div>
        </div>
      </div>
    </div>
  </section>
</main>
<footer>Made with love by humans and agents at <a href="https://fnctn.io">FNCTN.io</a></footer>
<script>
const FACT_LABELS = {
  caller_name: "Caller",
  reporter_role: "Role",
  policy_id: "Policy",
  plate: "Plate",
  vehicle: "Vehicle",
  vehicle_drivable: "Drivable",
  location: "Location",
  time_of_loss: "Time of loss",
  weather: "Weather",
  incident_type: "Incident",
  description: "Description",
  injuries: "Injuries",
  other_party: "Other party",
  police_on_scene: "Police on scene",
  witnesses: "Witnesses",
  photos_available: "Photos",
  claim_id: "Claim ID",
  stage: "Stage",
};

const fmt = (v) => {
  if (v === null || v === undefined || v === "") return null;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.length ? v.join(", ") : null;
  if (typeof v === "object") {
    const entries = Object.entries(v).filter(([, x]) => x !== null && x !== undefined && x !== "");
    return entries.length ? entries.map(([k, x]) => `${k}: ${fmt(x)}`).join(" · ") : null;
  }
  return String(v);
};

const fmtTime = (epoch) => {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  const now = Date.now() / 1000;
  const ago = now - epoch;
  if (ago < 60) return "just now";
  if (ago < 3600) return `${Math.floor(ago / 60)}m ago`;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

let sessions = []; // newest first
let activeId = null;
let userPickedActive = false;

const sessionsListEl = document.getElementById("sessions-list");
const sessionsCountEl = document.getElementById("sessions-count");
const detailTitle = document.getElementById("detail-title");
const detailSub = document.getElementById("detail-sub");
const factsEl = document.getElementById("facts");
const transcriptEl = document.getElementById("transcript");
const fraudFill = document.getElementById("fraud-fill");
const fraudScore = document.getElementById("fraud-score");
const fraudFlags = document.getElementById("fraud-flags");
const codemodeEl = document.getElementById("codemode");
const connDot = document.getElementById("conn-dot");
const connLabel = document.getElementById("conn-label");

const findSession = (id) => sessions.find(s => s.id === id);

const renderSessionsList = () => {
  sessionsCountEl.textContent = sessions.length;
  if (!sessions.length) {
    sessionsListEl.innerHTML = '<div class="empty-list">No calls yet.<br>Dial the number on <a href="/">the landing page</a> to see one appear here.</div>';
    return;
  }
  sessionsListEl.innerHTML = "";
  for (const s of sessions) {
    const el = document.createElement("div");
    el.className = "session" + (s.id === activeId ? " active" : "");
    const lastTurn = s.transcript && s.transcript.length ? s.transcript[s.transcript.length - 1] : null;
    const preview = lastTurn ? `${lastTurn.role === "user" ? "🧍" : "🎧"} ${lastTurn.text}` : "(no transcript yet)";
    const tag = s.status === "live"
      ? '<span class="live-tag">● live</span>'
      : '<span class="ended-tag">ended</span>';
    el.innerHTML = `
      <div class="row">
        <span class="id">#${s.short_id || (s.id || "").slice(-6)}</span>
        ${tag}
      </div>
      <div class="preview">${preview.replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]))}</div>
      <div class="row" style="margin-top:6px;">
        <span class="when">${fmtTime(s.started_at)}</span>
      </div>
    `;
    el.onclick = () => { userPickedActive = true; activeId = s.id; renderAll(); };
    sessionsListEl.appendChild(el);
  }
};

const renderFacts = (facts, prevFacts) => {
  factsEl.innerHTML = "";
  for (const [key, label] of Object.entries(FACT_LABELS)) {
    const v = fmt(facts[key]);
    const changed = prevFacts && fmt(prevFacts[key]) !== v;
    const el = document.createElement("div");
    el.className = "fact" + (changed ? " flash" : "");
    el.innerHTML = `<div class="k">${label}</div><div class="v ${v ? "" : "empty"}">${v ?? "—"}</div>`;
    factsEl.appendChild(el);
    if (changed) setTimeout(() => el.classList.remove("flash"), 1200);
  }

  const score = facts.fraud_score;
  const num = Number(score ?? 0);
  fraudScore.textContent = score === undefined || score === null ? "—" : num.toFixed(2);
  fraudFill.style.width = `${Math.min(100, Math.max(0, num * 100))}%`;
  fraudFill.style.background = num > 0.6 ? "var(--bad)" : num > 0.3 ? "var(--warn)" : "var(--good)";
  fraudFlags.innerHTML = "";
  for (const f of (facts.fraud_flags || [])) {
    const b = document.createElement("span");
    b.className = "badge";
    b.textContent = f;
    fraudFlags.appendChild(b);
  }
};

const renderTranscript = (turns) => {
  transcriptEl.innerHTML = "";
  if (!turns || !turns.length) {
    transcriptEl.innerHTML = '<div class="empty-msg">Live transcript will appear here turn by turn.</div>';
    return;
  }
  for (const t of turns) {
    const el = document.createElement("div");
    el.className = `turn ${t.role}`;
    el.innerHTML = `
      <div class="role">${t.role === "user" ? "U" : "R"}</div>
      <div class="body">${t.text.replace(/[<>&]/g, c => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c]))}</div>
    `;
    transcriptEl.appendChild(el);
  }
  transcriptEl.scrollTop = transcriptEl.scrollHeight;
};

const renderCodemode = (msg) => {
  codemodeEl.innerHTML = `
    <div class="fact"><div class="k">Naive tokens</div><div class="v">${msg.naive_tokens ?? "—"}</div></div>
    <div class="fact"><div class="k">Code-mode tokens</div><div class="v">${msg.codemode_tokens ?? "—"}</div></div>
    <div class="fact"><div class="k">Saved</div><div class="v">${msg.saved_pct ?? "—"}%</div></div>
    <div class="fact"><div class="k">Last result</div><div class="v">${msg.ok ? "ok" : "error"}</div></div>
  `;
};

let _prevFacts = null;
const renderActive = () => {
  const s = findSession(activeId);
  if (!s) {
    detailTitle.textContent = "Waiting for the next call…";
    detailSub.textContent = "When a call connects, extracted facts and the live transcript will appear here.";
    renderFacts({}, null);
    renderTranscript([]);
    return;
  }
  detailTitle.textContent = `Call #${s.short_id || s.id.slice(-6)} ${s.status === "live" ? "· live" : "· ended"}`;
  detailSub.textContent = `Started ${fmtTime(s.started_at)}${s.ended_at ? ` · ended ${fmtTime(s.ended_at)}` : ""}`;
  renderFacts(s.facts || {}, _prevFacts);
  _prevFacts = s.facts || {};
  renderTranscript(s.transcript || []);
};

const renderAll = () => { renderSessionsList(); renderActive(); };

const upsertSession = (incoming) => {
  const idx = sessions.findIndex(s => s.id === incoming.id);
  if (idx >= 0) {
    sessions[idx] = { ...sessions[idx], ...incoming };
  } else {
    sessions.unshift(incoming);
    sessions = sessions.slice(0, 20);
  }
  // Auto-follow new live calls unless the user clicked a specific one
  if (!userPickedActive || incoming.status === "live") {
    activeId = incoming.id;
  }
};

// Hydrate from server snapshot
fetch("/codemode/dashboard/state").then(r => r.json()).then(s => {
  sessions = (s.sessions || []).map(x => ({ ...x }));
  if (sessions.length && !activeId) activeId = sessions[0].id;
  renderAll();
}).catch(() => renderAll());

let ws;
const connect = () => {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { connDot.classList.add("live"); connLabel.textContent = "connected"; };
  ws.onclose = () => { connDot.classList.remove("live"); connLabel.textContent = "reconnecting…"; setTimeout(connect, 1500); };
  ws.onerror = () => { connDot.classList.remove("live"); };
  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch { return; }
    if (msg.type === "session_started") {
      upsertSession(msg.session);
      userPickedActive = false;
      renderAll();
    } else if (msg.type === "session_ended") {
      upsertSession(msg.session);
      renderAll();
    } else if (msg.type === "session_facts") {
      const s = findSession(msg.id);
      if (s) { s.facts = { ...(s.facts || {}), ...msg.facts }; renderAll(); }
    } else if (msg.type === "session_transcript") {
      const s = findSession(msg.id);
      if (s) {
        s.transcript = [...(s.transcript || []), msg.item];
        renderAll();
      }
    } else if (msg.type === "codemode_stats") {
      renderCodemode(msg);
    }
  };
};
connect();

// Refresh "X minutes ago" labels every 30s
setInterval(renderSessionsList, 30000);
</script>
</body>
</html>"""


@app.get("/")
def landing() -> Response:
    return Response(content=_LANDING_HTML.replace("__PHONE__", _PHONE_DISPLAY), media_type="text/html")


@app.get("/dashboard")
def dashboard_page() -> Response:
    return Response(content=_DASHBOARD_HTML, media_type="text/html")


@app.api_route("/twiml/inbound", methods=["GET", "POST"])
def twiml_inbound() -> Response:
    """Twilio fetches this when +49 30 4243 1626 rings.

    Returns TwiML that opens a Media Streams WebSocket to /ws/twilio.
    PUBLIC_HOST is the cloudflared tunnel (or any TLS-capable hostname).
    Twilio requires `wss://` and a TLS-terminated public endpoint.
    """
    public_host = os.getenv("PUBLIC_HOST", "throwing-fitted-nut-strange.trycloudflare.com")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Connect>
    <Stream url="wss://{public_host}/ws/twilio" />
  </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


# --------- Health ---------


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"ok": "yes"}


@app.get("/__diag")
def diag_endpoint() -> dict:
    return {"events": diag_snapshot()}


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("HUD_WS_PORT", "8765"))
    uvicorn.run(app, host="0.0.0.0", port=port)
