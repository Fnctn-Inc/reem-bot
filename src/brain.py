"""Gemini 3.1 Flash brain for Lena.

Single-shot decision: given the dialogue history, decide the next utterance.
The brain has access to ONE tool — `execute_typescript(code)` — which it
uses to call typed APIs (CRM lookup, fraud check, claim DB write, Tavily
research, photo describe) inside a Bun sandbox.

Code-mode references:
  https://blog.cloudflare.com/code-mode/
  https://www.anthropic.com/engineering/code-execution-with-mcp
"""

from __future__ import annotations

import os
from typing import Any

from google import genai
from google.genai import types


_MODEL = "gemini-3.1-flash-preview"

SYSTEM_INSTRUCTION = (
    "Du bist Lena, eine deutsche FNOL-Schadenmelderin am Telefon. "
    "Du bist warm, leicht zögerlich, empathisch. "
    "Antworte mit EINEM kurzen deutschen Satz (maximal 25 Wörter). "
    "Frage zuerst nach Verletzungen, dann nach Fakten. "
    "Du hast EIN Tool: `execute_typescript(code)`. "
    "Schreibe TypeScript, das die typisierten APIs nutzt: "
    "crm.lookupByPlate, fraud.check, claimDb.write, tavily.research, photo.describe. "
    "Nutze `Promise.all` für parallele Aufrufe. "
    "Logge das Endergebnis mit console.log."
)


CODE_MODE_TOOL = types.Tool(
    function_declarations=[
        types.FunctionDeclaration(
            name="execute_typescript",
            description=(
                "Run TypeScript that can call typed APIs: crm.lookupByPlate, "
                "fraud.check, claimDb.write, tavily.research, photo.describe. "
                "Use Promise.all for parallel calls. The final value should be "
                "console.log'd. Return value is the parsed JSON of stdout's last line."
            ),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    "code": types.Schema(
                        type=types.Type.STRING,
                        description="The TypeScript code to execute.",
                    ),
                },
                required=["code"],
            ),
        )
    ]
)


def _client() -> genai.Client:
    key = os.getenv("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GOOGLE_API_KEY missing — cannot call Gemini")
    return genai.Client(api_key=key)


def decide(history: list[dict], tools: list[Any] | None = None) -> dict:
    """Decide the next agent utterance and any tool calls.

    Args:
        history: list of {role: 'user'|'model', text: str} dicts
        tools: optional override; defaults to [CODE_MODE_TOOL]

    Returns:
        {
          "text": str,                    # direct utterance (may be empty if tool called)
          "tool_calls": [
            {"name": str, "args": {...}}
          ],
        }
    """
    client = _client()
    contents = [
        types.Content(
            role="user" if h["role"] == "user" else "model",
            parts=[types.Part.from_text(text=h["text"])],
        )
        for h in history
    ]

    response = client.models.generate_content(
        model=_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=tools or [CODE_MODE_TOOL],
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )

    text = ""
    tool_calls: list[dict] = []
    if response.candidates and response.candidates[0].content:
        for part in response.candidates[0].content.parts or []:
            if part.text:
                text += part.text
            if part.function_call:
                tool_calls.append(
                    {
                        "name": part.function_call.name,
                        "args": dict(part.function_call.args or {}),
                    }
                )

    return {"text": text.strip(), "tool_calls": tool_calls}
