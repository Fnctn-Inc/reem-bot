import random

import pytest

from src.persona import (
    LenaState,
    advance_state,
    inject_disfluency,
    normalize_break_tags,
)


def test_starts_in_greeting():
    assert LenaState().phase == "greeting"


def test_greeting_with_stress_word_jumps_to_empathy():
    s = LenaState()
    s = advance_state(s, user_said="Mein Auto ist total kaputt, ich weiß nicht was ich tun soll")
    assert s.phase == "acknowledge_stress"
    assert s.next_utterance  # something said


def test_greeting_without_stress_offers_calm():
    s = LenaState()
    s = advance_state(s, user_said="Ich möchte einen Schaden melden")
    assert s.phase == "calm_offer"
    assert "verletzt" in s.next_utterance.lower()


def test_acknowledge_then_calm_offer():
    s = LenaState(phase="acknowledge_stress")
    s = advance_state(s, user_said="Ja, danke")
    assert s.phase == "calm_offer"


def test_calm_offer_collects_injury_negative():
    s = LenaState(phase="calm_offer")
    s = advance_state(s, user_said="Nein, mir geht's gut.")
    assert s.collected.injured is False
    assert s.phase == "collect_facts"


def test_collect_facts_captures_location():
    s = LenaState(phase="collect_facts")
    s = advance_state(s, user_said="A2 bei Hannover, Kilometer 250")
    assert "A2" in (s.collected.location or "")
    assert s.phase == "photo_intake"


def test_photo_intake_then_wrap():
    s = LenaState(phase="photo_intake")
    s = advance_state(s, user_said="OK, ich schicke ein Foto")
    assert s.phase == "wrap"


LONG_LINE = (
    "Ich notiere das jetzt für Sie und schicke Ihnen gleich eine Bestätigung."
)


def test_disfluency_injected_when_probability_one():
    random.seed(0)
    out = inject_disfluency(LONG_LINE, probability=1.0)
    assert any(t in out for t in ("ähm", "also", "einen Moment", "lass mich"))


def test_disfluency_skipped_when_probability_zero():
    out = inject_disfluency(LONG_LINE, probability=0.0)
    assert out == LONG_LINE


def test_disfluency_skipped_for_short_utterances():
    """Short greetings/confirmations stay untouched even at p=1.0; fillers there sound fake."""
    random.seed(0)
    out = inject_disfluency("Sind Sie verletzt?", probability=1.0)
    assert out == "Sind Sie verletzt?"


def test_disfluency_prefers_last_comma_so_opener_stays_clean():
    random.seed(0)
    out = inject_disfluency(
        "Hallo, hier ist Reem vom Schadenservice, sind Sie selbst okay?",
        probability=1.0,
    )
    # Opener phrase still readable.
    assert "Hallo," in out and "hier ist Reem" in out
    # A filler word lands somewhere after the opener.
    assert any(t in out for t in ("ähm", "also", "einen Moment", "lass mich"))


def test_normalize_canonical_tag_is_unchanged():
    """The exact form Gradium expects passes through with no surgery."""
    canonical = '<break time="0.5s" />'
    assert canonical in normalize_break_tags(f"Oh nein. {canonical} Das tut mir leid.")


def test_normalize_rewrites_single_quoted_tags():
    """Gemini drifted to single quotes when the prompt examples had them.
    Gradium reads single-quoted tags aloud — we must rewrite to double."""
    out = normalize_break_tags("Oh nein. <break time='0.5s'/> Das tut mir leid.")
    assert "'" not in out
    assert '<break time="0.5s" />' in out


def test_normalize_handles_missing_space_before_slash():
    """`<break time="0.5s"/>` (no space) — rewrite to canonical with space."""
    out = normalize_break_tags('Mensch. <break time="0.5s"/> Heftig.')
    assert '<break time="0.5s" />' in out


def test_normalize_strips_unparseable_tags():
    """A <break> with no/garbage time attr can't be rendered — drop it."""
    assert "<break" not in normalize_break_tags("Hmm. <break/> ja.")
    assert "<break" not in normalize_break_tags("Hmm. <break foo='bar'/> ja.")


def test_normalize_clamps_absurd_durations():
    """Hallucinated 9000s tag would deadlock the call — clamp to 1.5s."""
    out = normalize_break_tags('Okay. <break time="9000s" /> Bye.')
    assert '<break time="1.5s" />' in out


def test_inject_disfluency_normalizes_llm_break_tags():
    leaked = "Oh nein. <break time='0.5s'/> Das tut mir wirklich sehr leid darum."
    out = inject_disfluency(leaked, probability=0.0)
    assert "'" not in out
    assert '<break time="0.5s" />' in out
