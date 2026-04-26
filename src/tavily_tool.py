"""Tavily Research API wrapper with a strict claim-context output schema.

Used both directly by the agent (in-call lookups) and as the `tavily.research`
tool exposed to model-written TypeScript via the code-mode sandbox.
"""

from __future__ import annotations

import os
from typing import Any

from tavily import TavilyClient


CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim_taxonomy": {
            "type": "string",
            "description": "Standard German P&C claim category for this incident.",
        },
        "fraud_red_flags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Domain-specific fraud indicators worth probing.",
        },
        "missing_facts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Facts a German FNOL agent should still ask for.",
        },
    },
    "required": ["claim_taxonomy", "fraud_red_flags", "missing_facts"],
}


def _client() -> TavilyClient:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise RuntimeError("TAVILY_API_KEY missing — cannot call Tavily Research")
    return TavilyClient(api_key=key)


def lookup_claim_context(description: str) -> dict:
    """Look up domain context for a German fender-bender claim.

    Returns a dict matching CLAIM_SCHEMA. On failure, returns a stub so the
    agent stays available even when Tavily is unreachable.
    """
    try:
        client = _client()
    except RuntimeError:
        return _stub(description)

    try:
        result = client.research(
            input=(
                f"German P&C insurance fender-bender claim. Description: {description}. "
                f"What is the standard claim taxonomy, fraud red-flags, and missing facts?"
            ),
            model="mini",
            output_schema=CLAIM_SCHEMA,
        )
        # Tavily Research returns {"answer": <obj>, "sources": [...]} when output_schema set
        return result.get("answer") or _stub(description)
    except Exception:
        return _stub(description)


def _stub(description: str) -> dict:
    """Last-resort fallback so the call doesn't fail mid-conversation."""
    return {
        "claim_taxonomy": "Kfz-Haftpflicht / Kollisionsschaden",
        "fraud_red_flags": [
            "neue Kratzer ohne Vorgeschichte",
            "fehlender Unfallort",
            "kein Polizeibericht trotz hoher Schadenssumme",
        ],
        "missing_facts": [
            "Datum und Uhrzeit",
            "Genauer Unfallort (Autobahn-Kilometer)",
            "Kennzeichen des Unfallgegners",
            "Verletzungen (ja/nein)",
            "Foto vom Schaden",
        ],
    }
