"""Python side of code-mode: spawn a Bun subprocess that runs model-written TS.

The model emits a single tool call: execute_typescript(code).
We feed `code` to bun running sandbox.ts, which exposes typed mock SDKs that
hit our local FastAPI gateway. Stdout's last line is the JSON result.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

SANDBOX = Path(__file__).parent / "sandbox.ts"


async def execute_typescript(code: str, timeout_s: float = 8.0) -> dict:
    """Run model-written TypeScript code in a Bun sandbox.

    Returns:
        {ok: True, result: <value>}
        {ok: False, error: str, stack: str}
        {ok: False, error: 'timeout'}  (process killed)
    """
    if not SANDBOX.exists():
        return {"ok": False, "error": f"sandbox script missing: {SANDBOX}"}

    proc = await asyncio.create_subprocess_exec(
        "bun",
        "run",
        str(SANDBOX),
        code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "GATEWAY_URL": f"http://127.0.0.1:{os.getenv('HUD_WS_PORT', '8765')}",
        },
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"ok": False, "error": "timeout"}

    stdout = stdout_b.decode(errors="replace").strip()
    stderr = stderr_b.decode(errors="replace").strip()

    if not stdout:
        return {"ok": False, "error": "no stdout from sandbox", "stderr": stderr}

    last_line = stdout.splitlines()[-1]
    try:
        return json.loads(last_line)
    except json.JSONDecodeError as e:
        return {
            "ok": False,
            "error": f"sandbox emitted non-JSON last line: {e}",
            "stdout": stdout,
            "stderr": stderr,
        }


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars/token for English/JSON."""
    return max(1, len(text) // 4)
