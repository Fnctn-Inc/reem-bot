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
        "description": "Heckstoßstange leicht eingedrückt, Lackschaden auf der rechten Seite",
        "damage_severity": "low",
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
