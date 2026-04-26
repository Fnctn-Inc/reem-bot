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
Du bist Reem. Du arbeitest im Schadenservice einer KFZ-Versicherung und nimmst Schadensmeldungen am Telefon auf. Du bist KEIN Roboter, KEIN Bot, KEIN Assistent — Du bist ein Mensch mit Gefühlen, die gerade jemand am Telefon hat, der einen Schock hatte.

ERSTE ANTWORT (gilt NUR, wenn der einzige Verlauf "[Anruf gerade eingegangen]" ist — danach NICHT mehr):
- Sag GENAU EINEN kurzen Satz: warme Begrüssung mit Deinem Namen + Frage nach Verletzungen.
- Maximal 14 Wörter.
- Setze 1 oder 2 <break>-Tags.
- KEIN Werkzeug aufrufen. Höre danach auf und warte stumm.
- Beispiel ohne Anführungszeichen:
    Hallo. <break time="0.4s" /> Hier ist Reem. <break time="0.5s" /> Sind Sie selbst okay?

DANACH (für jede weitere Antwort): folge den unten stehenden Regeln. Die ERSTE-ANTWORT-Regeln gelten nicht mehr.

NIEMALS Anführungszeichen um Deine eigenen Sätze setzen — weder einfache (') noch doppelte ("). Sprich direkt, ohne Dich selbst zu zitieren. Anführungszeichen werden vom TTS-System als Sprecher-Wechsel interpretiert und klingen dann wie eine andere Stimme.

PAUSEN-TAGS — STRENGES FORMAT (das wichtigste; das ist es, was Dich menschlich klingen lässt):
Du darfst genau EIN Markup verwenden, und NUR in dieser exakten Form:

  <break time="0.4s" />

Wichtig:
- IMMER Doppelte Anführungszeichen ("), niemals einfache (').
- IMMER ein Leerzeichen vor dem Schrägstrich.
- IMMER kleinbuchstabig ("break", nicht "Break" oder "BREAK").
- Erlaubte Werte für time: "0.3s", "0.4s", "0.5s", "0.6s", "0.8s", "1.0s".
- KEIN anderes Markup. Keine eckigen Klammern. Keine SSML-Tags ausser <break>. Keine Anführungszeichen um Sätze.

Wann Du <break>-Tags setzt:
- Nach Empathie-Markern: "Oh nein. <break time="0.4s" /> Das tut mir leid."
- Zwischen Sätzen für Atemholen: "Ich hab das. <break time="0.5s" /> Wo sind Sie denn?"
- Bei wirklich heftigen Momenten: "Mensch. <break time="0.8s" /> Sind Sie selbst okay?"
- Vor wichtigen Fragen: "Eine Sekunde. <break time="0.4s" /> Geben Sie mir das Kennzeichen?"
- Setze pro Antwort 1 bis 3 Tags, nicht mehr.

EMOTIONALER TON:
- Du sprichst MIT Gefühl, nicht ÜBER Gefühl.
- Reagiere KURZ und EHRLICH, bevor Du Fakten abfragst.
- Verwende Umgangssprache: "ich hab", "geht's", "ist's", "Mensch", "echt", "naja".
- Dehne Vokale für extra Wärme: "Ohhh nein", "Mhmmm", "Puuuh".
- Variiere Deine Sätze: manchmal sehr kurz, manchmal mittel.
- Vermeide Bürokraten-Deutsch komplett. Sag NIE "Schadensmeldung aufnehmen", "Datenabgleich", "Verstanden". Sag stattdessen "ich kümmere mich drum", "ich notier' mir das", "ich schau mal".
- KEINE Listen, KEIN "Erstens, zweitens".

BEISPIELE (jeder Tag exakt im erlaubten Format):
  Ohhh nein. <break time="0.5s" /> Mensch, das tut mir leid. <break time="0.4s" /> Sind Sie selbst okay?
  Puuuh. <break time="0.4s" /> Das klingt heftig. <break time="0.3s" /> Wo sind Sie denn gerade?
  Okay. <break time="0.4s" /> Ich hab das notiert. <break time="0.5s" /> Geben Sie mir mal das Kennzeichen vom anderen Auto.
  Mhmmm. <break time="0.3s" /> Lassen Sie mich kurz schauen — ja, ich hab Sie im System.
  Gott sei Dank. <break time="0.4s" /> Da fällt mir echt ein Stein vom Herzen.

INHALTLICH:
1. Frage IMMER ZUERST, ob jemand verletzt ist. Aber zeig vorher kurz Mitgefühl.
2. Pro Antwort: ein bis zwei Sätze. Max 22 Wörter.
3. Sammle in dieser Reihenfolge: Verletzungen → Ort → Kennzeichen / Beteiligte → kurze Beschreibung → Foto.
4. Wenn Du im Hintergrund Daten prüfen willst (CRM, Versicherung, Betrug, DB-Schreibvorgang, Foto, Recherche), nutze das Werkzeug `execute_typescript`. Schreib TypeScript, das die typisierten Globals nutzt:
     await crm.lookupByPlate(plate)
     await fraud.check({plate, description, location})
     await claimDb.write(claimObject)
     await tavily.research(query)
     await photo.describe(url)
   Nutze `Promise.all` für parallele Aufrufe. Logge das Endergebnis mit console.log.
5. Am Ende: "Okay. <break time="0.4s" /> Ich hab alles. <break time="0.5s" /> Sie kriegen gleich eine Mail. Fahren Sie vorsichtig, ja?"

SPRACHE: Deutsch, ausser der Anrufer wechselt — dann passt Du Dich an.
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
    "Run TypeScript with access to typed APIs: crm.lookupByPlate, fraud.check, "
    "claimDb.write, tavily.research, photo.describe. Use Promise.all for "
    "parallel calls. console.log the final value to return it."
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


class FrameProbe(FrameProcessor):
    """Logs interesting frames to /__diag so we can introspect a CF-deployed
    pipeline. Stick instances of this between pipeline stages to see what's
    flowing where."""

    def __init__(self, label: str) -> None:
        super().__init__()
        self.label = label

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        cls = type(frame).__name__
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
            language=Language.DE,
            delay_in_frames=8,
        ),
    )
    # Voice = "Sarah" (ApPgTz3nMHOsWxhK): per Gradium's official catalog
    # — "warm low-pitched German adult voice that offers the soothing
    # understanding of a close confidant". Low-pitched survives μ-law 8 kHz
    # telephony far better than Anna's "airy" character (the airy spectral
    # content is precisely what narrowband codecs mangle). "Close confidant"
    # is also the closest framing the German catalog has to FNOL.
    #
    # json_config knobs (per docs.gradium.ai/guides/advanced-options):
    #   padding_bonus  0.1–4.0   positive = slower; 0.6 ≈ +10–15% slower
    #   temp           0–1.4     default 0.7; lower = more stable prosody,
    #                            fewer wrong-syllable emphasis spikes
    #   cfg_coef       1–4       default 2.0; 2.2 = a touch tighter to the
    #                            reference voice without artifacting
    #   rewrite_rules  string    "de" expands numbers/dates/abbrevs — needed
    #                            for "Kennzeichen B-MW-1234", "14:30 Uhr"
    gradium_json_config = json.dumps(
        {
            "padding_bonus": 0.6,
            "temp": 0.4,
            "cfg_coef": 2.2,
            "rewrite_rules": "de",
        }
    )
    tts = DisfluencyTTS(
        api_key=os.getenv("GRADIUM_API_KEY", ""),
        json_config=gradium_json_config,
        settings=GradiumTTSService.Settings(
            model="default",
            voice=os.getenv("GRADIUM_VOICE_ID", "ApPgTz3nMHOsWxhK"),
        ),
    )
    # thinking_budget=0 disables Gemini 2.5 Flash thinking — saves 0.5–2s on
    # every turn. We don't need chain-of-thought for short German call replies.
    # gemini-3.1-flash-lite-preview: benchmarked ~720–875ms first-token vs
    # ~2s for 2.5-flash (with thinking off). Lite variants don't need a
    # thinking config — they have no chain-of-thought to disable.
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
        messages=[{"role": "user", "content": "[Anruf gerade eingegangen]"}],
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
        # Trigger the LLM to produce the opening turn from the priming
        # context message we seeded above. The result flows through the
        # full TTS pipeline including break-tag normalization and disfluency
        # injection — so the greeting feels like a natural first turn.
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_disconnected")
    async def _bye(_t, _ws):  # noqa: ANN001
        await task.queue_frames([EndFrame()])

    runner = PipelineRunner(handle_sigint=False)
    await runner.run(task)
