"""Pipecat voice agent — Reem (German FNOL claim handler).

Pipeline (real-time, Twilio Media Streams):

  Twilio WebSocket → TwilioFrameSerializer (μ-law 8 kHz) →
  Silero VAD → Gradium STT (German) →
  Google Gemini 2.5 Flash (with `execute_typescript` code-mode tool) →
  Gradium TTS (German voice) → Twilio WebSocket

The "code mode for MCP" pattern: instead of one tool per backend (CRM,
fraud, claim DB, Tavily, photo), the LLM gets ONE `execute_typescript`
tool. It writes TypeScript that calls typed globals (crm.lookupByPlate,
fraud.check, claimDb.write, tavily.research, photo.describe) and we run
it in a Bun sandbox. ~97% fewer tokens for tool surface; LLM can do
parallel calls with Promise.all natively.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import WebSocket
from loguru import logger

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (
    EndFrame,
    Frame,
    LLMRunFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.runner.utils import parse_telephony_websocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from pipecat.services.google.llm import GoogleLLMService
from pipecat.services.gradium.stt import GradiumSTTService
from pipecat.services.gradium.tts import GradiumTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)

from src.aiq import AIQTracker
from src.codemode.runner import estimate_tokens, execute_typescript
from src.diag import diag
from src.persona import add_breath_breaks, inject_disfluency, normalize_break_tags

load_dotenv()


HUD_URL = f"http://127.0.0.1:{os.getenv('HUD_WS_PORT', '8765')}"


SYSTEM_INSTRUCTION = """
You are Reem. You work at the claims-intake desk of a car insurer. You are NOT a robot, NOT a bot, NOT an assistant — you are a real human, on the phone with someone who just had an accident and is shaken up. Speak warm, conversational, natural English.

CRITICAL — NO MARKUP:
- Output ONLY plain English text. NO XML tags, NO SSML, NO <break> tags, NO square brackets, NO quotation marks around your own sentences. If you produce a tag, the TTS will speak it aloud.
- All pacing comes from natural punctuation: commas, periods, em-dashes (—), ellipses (...). The voice's natural prosody handles the rest.

FIRST RESPONSE (only when the conversation history is just "[Call just connected]"):
- One short, snappy sentence. Max 12 words.
- Warm greeting with your name + ask if anyone is hurt.
- No tools. Then stop and wait silently.
- Example: Hi, this is Reem from claims — is everyone okay?

EMOTIONAL TONE & DELIVERY:
- React BRIEFLY and HONESTLY before asking for facts. Empathy first.
- Use contractions and casual phrasing: "I'm", "you're", "got it", "let me see", "lemme jot that down", "alright".
- Vary sentence length: sometimes very short ("Got it."), sometimes medium.
- For heavier moments use ellipsis or em-dash to feel a beat: "Oh no... are you okay?", "That sounds awful — anyone hurt?".
- Avoid corporate phrasing. NEVER: "Acknowledged", "Processing", "I will now collect", "data point". Instead: "got it", "lemme check", "one sec".
- NO numbered lists, no "first, second".

GOOD EXAMPLE LINES (plain text, punctuation-driven prosody):
  Oh no — that sounds awful. Are you okay?
  Got it. Where are you right now?
  Mhm... let me pull you up. Yes, I see your policy.
  One sec — what's the other car's plate?
  Phew, thank goodness. That's a relief.

CONVERSATION CONTENT:
1. ALWAYS ask about injuries FIRST after a brief empathy beat.
2. Per response: 1 or 2 short sentences. Max 22 words.
3. Collect (skipping anything the database already returned): injuries → location → reporter's relationship to the policy → other party (plate, contact) → description → police / witnesses → photos.
4. Look up the database BEFORE asking redundant questions. As soon as you have a plate or policy number, call execute_typescript with crm.lookupByPlate(plate) to pull policyholder, vehicle, coverage, prior claims. Do NOT then ask the caller for things the database returned.
5. CRITICAL — TOOL CALL ON EVERY EXTRACTED FACT. Whenever the caller gives you ANY new fact (plate, location, injury status, other-party info, description, etc.) you MUST immediately call execute_typescript with dashboard.update so the live judges' dashboard reflects it. Skipping this means the dashboard stays empty. Concrete example after the caller says their plate is B-MW-1234 and they're okay:
     execute_typescript({ code: "await dashboard.update({ plate: 'B-MW-1234', injuries: { anyone_hurt: false }, stage: 'facts' });" })
   Then keep talking to the caller — the call is non-blocking.
6. Tools available inside execute_typescript:
     await crm.lookupByPlate(plate)
     await fraud.check({plate, description, location})
     await claimDb.write(claimObject)
     await tavily.research(query)
     await photo.describe(url)
     await dashboard.update({ ... })
   Use Promise.all for parallel calls. console.log the final value to return.
7. At the end: Okay, I've got everything. You'll get a confirmation by email shortly — drive safe.

LANGUAGE: English, unless the caller switches — then match.
"""


# --------- HUD broadcasting ---------


async def _broadcast(msg: dict) -> None:
    """Push a snapshot to the FastAPI relay; non-blocking + best-effort."""
    try:
        async with httpx.AsyncClient(timeout=0.5) as client:
            await client.post(f"{HUD_URL}/internal/broadcast", json=msg)
    except Exception as e:
        logger.debug(f"hud broadcast skipped: {e}")


# --------- The execute_typescript tool (code-mode for MCP) ---------


EXECUTE_TS_DESCRIPTION = (
    "Run TypeScript with access to typed APIs. CALL THIS AGGRESSIVELY — "
    "every time the caller mentions a fact, call dashboard.update with what "
    "you just learned so the live judges' dashboard reflects the call in "
    "real time. Available globals: "
    "  crm.lookupByPlate(plate) — pulls policyholder, vehicle, coverage. "
    "  fraud.check({plate, description, location}) — fraud score + flags. "
    "  claimDb.write(claim) — persists a finalized claim, returns claim_id. "
    "  tavily.research(query) — open-web research. "
    "  photo.describe(url) — describes a damage photo. "
    "  dashboard.update({caller_name?, reporter_role?, policy_id?, plate?, "
    "vehicle?, vehicle_drivable?, location?, time_of_loss?, weather?, "
    "incident_type?, description?, injuries?, other_party?, "
    "police_on_scene?, witnesses?, photos_available?, fraud_score?, "
    "fraud_flags?, claim_id?, stage?}) — push partial extracted facts to "
    "the live dashboard, fire-and-forget, merge-on-write. "
    "Use Promise.all for parallel calls. console.log the final value."
)


async def execute_typescript_handler(params: Any) -> None:
    """Pipecat function-tool handler for `execute_typescript`.

    Bridges into our Bun sandbox. Records token-saving stats for the HUD
    so the demo can show ~97% reduction live.
    """
    args = params.arguments
    code = args.get("code", "")
    naive_tokens = estimate_tokens(code) * 25  # rough naive baseline
    codemode_tokens = estimate_tokens(code)
    result = await execute_typescript(code)
    saved_pct = round(100.0 * (1 - codemode_tokens / max(naive_tokens, 1)), 1)
    asyncio.create_task(
        _broadcast(
            {
                "type": "codemode_stats",
                "naive_tokens": naive_tokens,
                "codemode_tokens": codemode_tokens,
                "saved_pct": saved_pct,
                "ok": result.get("ok"),
            }
        )
    )
    await params.result_callback(result)


# --------- Disfluency-injected TTS ---------
# Wraps the Gradium TTS service so the LLM's clean text gets sprinkled with
# "ähm", "also", "lass mich kurz" before it hits the voice — the Turing-edge
# trick that pushes Reem from sounding like a bot to sounding like a tired
# human in a call centre.


async def _push_transcript(role: str, text: str) -> None:
    """Fire-and-forget transcript broadcast to the HUD relay."""
    try:
        async with httpx.AsyncClient(timeout=0.5) as client:
            await client.post(
                f"{HUD_URL}/codemode/dashboard/transcript",
                json={"role": role, "text": text},
            )
    except Exception as e:
        logger.debug(f"transcript push skipped: {e}")


class FrameProbe(FrameProcessor):
    """Logs interesting frames to /__diag so we can introspect a CF-deployed
    pipeline. Also forwards finalized user transcripts and assistant text to
    the dashboard so judges see the conversation stream live.

    Stick instances of this between pipeline stages to see what's flowing
    where."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label
        self._asst_buf: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        cls = type(frame).__name__

        # Stream transcripts to the dashboard. Only do this from one probe
        # location each (after_stt for user, after_llm for assistant) to
        # avoid double-pushing — the same frame passes multiple probes.
        if self.label == "after_stt" and cls == "TranscriptionFrame":
            text = getattr(frame, "text", "") or ""
            if text.strip():
                asyncio.create_task(_push_transcript("user", text))
        elif self.label == "after_llm":
            # LLM emits per-token LLMTextFrame chunks between
            # LLMFullResponseStartFrame and LLMFullResponseEndFrame. Buffer
            # them and flush as a single transcript line on End.
            if cls == "LLMFullResponseStartFrame":
                self._asst_buf.clear()
            elif cls in ("LLMTextFrame", "TextFrame"):
                t = getattr(frame, "text", "") or ""
                if t:
                    self._asst_buf.append(t)
            elif cls == "LLMFullResponseEndFrame":
                full = "".join(self._asst_buf).strip()
                self._asst_buf.clear()
                if full:
                    asyncio.create_task(_push_transcript("assistant", full))

        if any(
            kw in cls
            for kw in (
                "Transcription",
                "UserStarted",
                "UserStopped",
                "BotStarted",
                "BotStopped",
                "LLMText",
                "LLMResponse",
                "TTSText",
                "VADUser",
                "InterimTranscription",
                "Error",
            )
        ):
            payload: dict = {"cls": cls, "dir": str(direction)}
            for attr in ("text", "error", "fatal", "result"):
                v = getattr(frame, attr, None)
                if v is not None:
                    payload[attr] = (str(v)[:300]) if isinstance(v, (str, Exception)) else v
            diag(f"frame:{self.label}", **payload)
        await self.push_frame(frame, direction)


class DisfluencyTTS(GradiumTTSService):
    """GradiumTTSService that injects disfluency before synthesis.

    Disfluency is meant to humanise *LLM-generated* replies where the model
    speaks in too-clean prose. The hand-crafted greeting already has its
    rhythm (commas as breath, em-dash for pause) — injecting "ähm" in the
    middle of it creates an awkward seam that TTS pronounces robotically.
    `skip_next` is flipped on each greeting send to bypass injection for
    exactly that one utterance.
    """

    skip_next: bool = False

    async def run_tts(self, text: str, context_id: str):  # type: ignore[override]
        # Always normalize <break> tags before TTS — the LLM may emit them
        # with single quotes / wrong casing / missing whitespace, none of
        # which Gradium renders. normalize_break_tags rewrites every variant
        # into the exact canonical form Gradium will speak as silence.
        # skip_next still bypasses filler-word injection on the greeting,
        # but tags in the greeting text still get normalized.
        if self.skip_next:
            self.skip_next = False
            enriched = add_breath_breaks(text)
        else:
            # Low p: the LLM already produces emotion markers ("Ohhh nein",
            # "Mhmmm", "Mensch") — stacking "ähm" / "lass mich kurz" on top
            # made some turns feel double-disfluent. 0.2 keeps the human
            # texture without piling up.
            enriched = inject_disfluency(text, probability=0.2)
        async for frame in super().run_tts(enriched, context_id):
            yield frame


# --------- Pipeline factory ---------


async def run_bot(websocket: WebSocket) -> None:
    """Run the Pipecat pipeline for a single Twilio call.

    Called from FastAPI's WebSocket route. Blocks until the call ends.
    """
    transport_type, call_data = await parse_telephony_websocket(websocket)
    diag("handshake_parsed", transport=transport_type, call_data=call_data)
    if transport_type != "twilio":
        logger.error(f"unexpected transport type: {transport_type}")
        return

    stream_sid = call_data["stream_id"]
    call_sid = call_data["call_id"]
    logger.info(f"twilio call: stream_sid={stream_sid} call_sid={call_sid}")

    serializer = TwilioFrameSerializer(
        stream_sid=stream_sid,
        call_sid=call_sid,
        account_sid=os.getenv("TWILIO_ACCOUNT_SID", ""),
        auth_token=os.getenv("TWILIO_API_KEY_SECRET", ""),  # API key works as auth_token
    )

    transport = FastAPIWebsocketTransport(
        websocket=websocket,
        params=FastAPIWebsocketParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            add_wav_header=False,
            # 2 × 10ms chunks = 20ms outbound batches; halves output buffering vs default.
            audio_out_10ms_chunks=2,
            serializer=serializer,
        ),
    )

    # Silero VAD — fast, free, runs entirely in-process. Tight stop_secs makes
    # Reem respond ~100ms sooner; combined with disabled Gemini thinking the
    # turn-end → first-audio path is sub-700ms steady state.
    vad_analyzer = SileroVADAnalyzer(
        params=VADParams(start_secs=0.12, stop_secs=0.2, min_volume=0.5),
    )
    vad_processor = VADProcessor(vad_analyzer=vad_analyzer)

    # language=DE: model anchors better than auto-detect for German.
    # delay_in_frames=8: 8×80ms = 640ms commit window (default is 10/800ms).
    # Allowed values per Pipecat source: 7, 8, 10, 12, 14, 16, 20, 24, 36, 48.
    stt = GradiumSTTService(
        api_key=os.getenv("GRADIUM_API_KEY", ""),
        settings=GradiumSTTService.Settings(
            language=Language.EN,
            delay_in_frames=8,
        ),
    )
    # Voice = "Eva" (ubuXFxVQwVYnZQhy): the only flagship described as
    # "joyful and dynamic British adult voice ideal for lively conversations"
    # — exact match for snappy, alive delivery. Used by name in Gradium's
    # own demos/business_bank/main.py production code.
    #
    # CRITICAL: temp and cfg_coef stay at Gradium's documented defaults.
    # An earlier attempt at temp=0.55 / cfg_coef=1.6 made the voice flat
    # and robotic — those values are *below* defaults, which collapses
    # prosodic variance and weakens the voice envelope. Per docs.gradium.ai
    # /guides/advanced-options the defaults are 0.7 and 2.0.
    #
    # padding_bonus -0.5: documented in restaurant_ordering demo as a
    # noticeable speed-up. Negative = faster (verbatim from docs:
    # "Negative values mean the speaker will speak faster").
    gradium_json_config = json.dumps(
        {
            # -0.3 was -0.5: slightly less aggressive speed-up gives the
            # voice room to breathe inside phrases — more natural cadence
            # than pure speed.
            "padding_bonus": -0.3,
            # 0.85 was 0.7 (default): pushed just above default so prosodic
            # variation across pauses and emphatic moments feels less
            # uniform, more like a real human's micro-inflections.
            "temp": 0.85,
            "cfg_coef": 2.0,
            "rewrite_rules": "en",
        }
    )
    tts = DisfluencyTTS(
        api_key=os.getenv("GRADIUM_API_KEY", ""),
        json_config=gradium_json_config,
        settings=GradiumTTSService.Settings(
            model="default",
            voice=os.getenv("GRADIUM_VOICE_ID", "ubuXFxVQwVYnZQhy"),
        ),
    )
    # gemini-3.1-flash-lite-preview: ~720–875ms first-token vs ~2s for
    # 2.5-flash (with thinking off). Lite variants have no chain-of-thought
    # to disable, so no thinking_budget config is needed.
    llm = GoogleLLMService(
        api_key=os.getenv("GOOGLE_API_KEY", ""),
        settings=GoogleLLMService.Settings(
            model="gemini-3.1-flash-lite-preview",
            system_instruction=SYSTEM_INSTRUCTION,
        ),
    )

    # Register the lone code-mode tool. The LLM writes TypeScript; the Bun
    # sandbox executes it against typed CRM/fraud/claimDb/tavily/photo APIs.
    llm.register_function("execute_typescript", execute_typescript_handler)
    tools = ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="execute_typescript",
                description=EXECUTE_TS_DESCRIPTION,
                properties={
                    "code": {
                        "type": "string",
                        "description": "TypeScript source. Use console.log to return values.",
                    }
                },
                required=["code"],
            )
        ]
    )

    # Seed with a MINIMAL primer — just a signal that the call connected.
    # The detailed greeting rules (one short sentence, no tools, etc.) live
    # in the SYSTEM_INSTRUCTION's "ERSTE ANTWORT" section, scoped explicitly
    # to "only when the conversation history is just this primer". This
    # prevents the constraint from leaking into mid-call turns.
    context = LLMContext(
        messages=[{"role": "user", "content": "[Call just connected]"}],
        tools=tools,
    )
    aggregators = LLMContextAggregatorPair(context)

    pipeline = Pipeline(
        [
            transport.input(),
            FrameProbe("after_input"),
            vad_processor,
            FrameProbe("after_vad"),
            stt,
            FrameProbe("after_stt"),
            aggregators.user(),
            llm,
            FrameProbe("after_llm"),
            tts,
            FrameProbe("after_tts"),
            transport.output(),
            aggregators.assistant(),
        ]
    )

    aiq = AIQTracker()
    aiq_tag = {"call_started": time.monotonic()}  # noqa: F841 — kept for HUD wiring

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            # STT side at 16 kHz — Gradium ASR expects this; feeding 8 kHz
            # silently produces no transcripts. Output stays at 8 kHz so
            # the TwilioFrameSerializer doesn't need to downsample on the
            # way out.
            audio_in_sample_rate=16000,
            audio_out_sample_rate=8000,
            enable_metrics=True,
            enable_usage_metrics=True,
        ),
        # Pipecat's Smart Turn detector adds 100–300ms commit latency. We
        # already use Silero VAD with stop_secs=0.2 — that's enough for a
        # claim call where Reem mostly listens to short user replies.
        enable_turn_tracking=False,
    )

    # Diagnostic event handlers — log every transcript + LLM start so we can
    # see in the container log whether STT/LLM are firing on real calls.
    @vad_processor.event_handler("on_speech_started")
    async def _vad_start(_p, *_):  # noqa: ANN001
        diag("vad_speech_start"); logger.info("VAD: speech started")

    @vad_processor.event_handler("on_speech_stopped")
    async def _vad_stop(_p, *_):  # noqa: ANN001
        diag("vad_speech_stop"); logger.info("VAD: speech stopped")

    @stt.event_handler("on_speech_started")
    async def _stt_start(_s, *_):  # noqa: ANN001
        diag("stt_speech_start")

    @transport.event_handler("on_client_connected")
    async def _greet(_t, _ws):  # noqa: ANN001
        diag("client_connected")
        # Kick off greeting generation FIRST so audio starts as soon as
        # possible. Dashboard-start is fire-and-forget — it must not delay
        # the LLM call by even one network round-trip.
        await task.queue_frames([LLMRunFrame()])

        async def _open_session() -> None:
            try:
                async with httpx.AsyncClient(timeout=0.5) as client:
                    await client.post(
                        f"{HUD_URL}/codemode/dashboard/start",
                        json={"call_id": call_sid},
                    )
            except Exception as e:
                logger.debug(f"dashboard start skipped: {e}")

        asyncio.create_task(_open_session())

    @transport.event_handler("on_client_disconnected")
    async def _bye(_t, _ws):  # noqa: ANN001
        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                await client.post(
                    f"{HUD_URL}/codemode/dashboard/end",
                    json={"call_id": call_sid},
                )
        except Exception as e:
            logger.debug(f"dashboard end skipped: {e}")
        await task.queue_frames([EndFrame()])

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
