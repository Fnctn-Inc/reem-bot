"""Post-call structured claim extractor (Gemini Flash structured-output).

After the call ends, feed the entire transcript here to produce the FNOL JSON
record Inca expects: claim type, datetime, location, parties, plate(s),
damage description, photo URLs, claimant contact, severity flag.
"""

from __future__ import annotations

import json
import os
from typing import Any

from google import genai
from google.genai import types


CLAIM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim_type": {"type": "string"},
        "incident_time": {"type": "string"},
        "location": {"type": "string"},
        "injured": {"type": "boolean"},
        "other_party_plate": {"type": "string"},
        "claimant_name": {"type": "string"},
        "description": {"type": "string"},
        "damage_severity": {
            "type": "string",
            "enum": ["low", "medium", "high"],
        },
        "photo_urls": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["claim_type", "location", "injured", "description"],
}


_MODEL = "gemini-3.1-flash-preview"


def extract_claim(transcript: str) -> dict:
    """Extract a structured claim record from a call transcript.

    Transcript format is freeform; the model parses speaker turns.
    """
    key = os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY missing — cannot extract claim")

    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=_MODEL,
        contents=(
            "Extract structured claim data from this German FNOL call transcript. "
            "Use the schema. Use empty string for unknown text fields and false for "
            "unknown booleans rather than omitting them.\n\n"
            f"TRANSCRIPT:\n{transcript}"
        ),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CLAIM_SCHEMA,
        ),
    )
    text = response.text or "{}"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text, "_error": "non-JSON response"}
