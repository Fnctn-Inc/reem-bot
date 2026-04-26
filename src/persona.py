"""Lena's empathy state machine + disfluency injector.

The Turing-test edge: most voice agents are TOO crisp. Real humans on a
fender-bender call are warm, slightly disfluent, and ask about injury BEFORE
data. This module encodes that.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional


PHASES = (
    "greeting",
    "acknowledge_stress",
    "calm_offer",
    "collect_facts",
    "photo_intake",
    "wrap",
)


@dataclass
class CollectedFacts:
    incident_time: Optional[str] = None
    location: Optional[str] = None
    injured: Optional[bool] = None
    other_party_plate: Optional[str] = None
    description: Optional[str] = None
    photo_url: Optional[str] = None


@dataclass
class LenaState:
    phase: str = "greeting"
    next_utterance: str = ""
    collected: CollectedFacts = field(default_factory=CollectedFacts)


EMPATHY_LEXICON = (
    "Oh, das tut mir leid, das klingt wirklich stressig.",
    "Ich verstehe, das ist ein Schock. Atmen Sie einmal kurz durch, ich bin für Sie da.",
    "Ja, das ist ärgerlich. Wir kriegen das gemeinsam hin.",
)

DISFLUENCY_TOKENS = (
    "ähm",
    "also",
    "einen Moment",
    "lass mich kurz",
)

STRESS_WORDS = (
    "kaputt", "durch", "schock", "weiß nicht", "schlimm",
    "verletzt", "blut", "schrecklich", "furchtbar",
)


def advance_state(state: LenaState, user_said: str) -> LenaState:
    """Advance the state machine one step based on the user's last utterance."""
    user = user_said.lower()

    if state.phase == "greeting":
        if any(w in user for w in STRESS_WORDS):
            state.phase = "acknowledge_stress"
            state.next_utterance = random.choice(EMPATHY_LEXICON)
            return state
        state.phase = "calm_offer"
        state.next_utterance = (
            "Ich helfe Ihnen, das Ganze ruhig aufzunehmen. Sind Sie verletzt?"
        )
        return state

    if state.phase == "acknowledge_stress":
        state.phase = "calm_offer"
        state.next_utterance = "Sind Sie selbst verletzt, oder ist sonst jemand verletzt?"
        return state

    if state.phase == "calm_offer":
        state.collected.injured = not (
            "nein" in user or "kein" in user or "geht's gut" in user or "ok" in user
        )
        state.phase = "collect_facts"
        state.next_utterance = "Gut, das ist die wichtigste Frage. Wo sind Sie gerade?"
        return state

    if state.phase == "collect_facts":
        if not state.collected.location:
            state.collected.location = user_said.strip()
        state.phase = "photo_intake"
        state.next_utterance = (
            "Können Sie mir ein Foto vom Schaden an diese Nummer schicken?"
        )
        return state

    if state.phase == "photo_intake":
        state.phase = "wrap"
        state.next_utterance = (
            "Perfekt, ich habe alles. Sie bekommen in den nächsten Minuten "
            "eine Bestätigung per E-Mail. Fahren Sie vorsichtig."
        )
        return state

    state.next_utterance = "Vielen Dank, einen schönen Tag noch."
    return state


# Matches every <break ...> variant: with or without time, single/double/no
# quotes, missing-space self-close, weird casing. The callback decides
# whether to rewrite to canonical form or drop the tag entirely.
_BREAK_TAG_RE = re.compile(r"<\s*break\b[^>]*?/?\s*>", re.IGNORECASE)
_TIME_ATTR_RE = re.compile(
    r"""time\s*=\s*['"]?(?P<secs>\d+(?:\.\d+)?)""",
    re.IGNORECASE | re.VERBOSE,
)


def normalize_break_tags(text: str) -> str:
    """Rewrite every ``<break>`` variant into Gradium's canonical form.

    Gradium's SSML parser is picky: it accepts ``<break time="0.5s" />``
    (lowercase tag, double-quoted time, space before self-closing slash) but
    speaks tags with single quotes / missing space / weird casing aloud as
    text. The LLM doesn't reliably emit the canonical form — examples in
    the prompt with ``time='0.4s'`` leak into single-quoted output. So we
    normalize on the way out: extract the time value, drop the rest of the
    tag, re-emit in canonical form. Tags without a parseable time get
    stripped entirely.
    """

    def _rewrite(match: re.Match) -> str:
        time_match = _TIME_ATTR_RE.search(match.group(0))
        if time_match is None:
            return ""
        try:
            value = float(time_match.group("secs"))
        except ValueError:
            return ""
        # Clamp so a hallucinated <break time="9000s"/> can't deadlock a call.
        value = max(0.1, min(value, 1.5))
        return f' <break time="{value:.1f}s" /> '

    return _BREAK_TAG_RE.sub(_rewrite, text)


def strip_self_quoting(text: str) -> str:
    """Strip leading/trailing quote characters Gemini sometimes wraps its
    own response in (mimicking quoted prompt examples).

    Why: Gradium treats quoted text as a speaker change and renders it in a
    different voice — a single response wrapped in ``'...'`` came out of
    the speaker as two competing voices. We normalize by stripping any
    matched outer quote pair and any naked leading/trailing quote.
    """
    text = text.strip()
    if len(text) >= 2 and text[0] in "\"'“”‘’" and text[-1] in "\"'“”‘’":
        return text[1:-1].strip()
    return text.strip("\"'“”‘’").strip()


def strip_break_tags(text: str) -> str:
    """Remove every ``<break>`` tag — used only on diagnostic / test paths.

    Production uses ``normalize_break_tags`` instead: we want the breaks
    to make it through to Gradium, just in canonical form.
    """
    return _BREAK_TAG_RE.sub("", text)


def add_breath_breaks(text: str) -> str:
    """Translate authorial pause-cue characters (em-dash, ellipsis) to
    Gradium ``<break>`` tags, then normalize to canonical form.

    The LLM is the primary source of breaks — it decides where pauses
    belong with full semantic context. This function exists so the canned
    greeting (and any other hand-crafted text) gets the same break-tag
    treatment without us having to hand-write the tags inline.
    """
    text = text.replace("—", ' <break time="0.5s" /> ')
    text = text.replace("…", ' <break time="0.6s" /> ')
    text = text.replace("...", ' <break time="0.6s" /> ')
    return normalize_break_tags(text)


def inject_disfluency(text: str, probability: float = 0.4) -> str:
    """Normalize any LLM-emitted break tags + (optionally) add a filler word.

    Two passes:
      1) Always normalize ``<break>`` tags — the LLM's output may have
         single-quoted attrs, missing whitespace, or other variants Gradium
         won't render. We rewrite to the canonical form on the way out.
      2) On utterances ≥10 words, ~p% of the time, drop in a German filler
         ("ähm" / "also" / "einen Moment" / "lass mich kurz") near the
         LAST comma, with a short break tag right after.
    """
    text = strip_self_quoting(normalize_break_tags(text))

    if random.random() > probability:
        return text
    if len(text.split()) < 10:
        return text
    token = random.choice(DISFLUENCY_TOKENS)
    if ", " in text:
        idx = text.rfind(", ")
        return f'{text[:idx]}, {token} <break time="0.4s" /> {text[idx+2:]}'
    return f'{token} <break time="0.4s" /> {text}'
