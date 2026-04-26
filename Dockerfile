FROM python:3.11-slim AS base

# System deps for Pipecat (audio libs, build tools for onnxruntime/torch wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl unzip git build-essential \
 && rm -rf /var/lib/apt/lists/*

# Install bun (~30MB) — needed by src/codemode/runner.py for the TypeScript sandbox
RUN curl -fsSL https://bun.sh/install | bash \
 && cp /root/.bun/bin/bun /usr/local/bin/bun \
 && bun --version

# Install uv (Python package manager — much faster than pip, matches local dev)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
 && cp /root/.local/bin/uv /usr/local/bin/uv \
 && uv --version

WORKDIR /app

# Install Python deps first (better Docker layer caching: deps only re-install on lockfile change)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# Pre-warm Silero VAD ONNX so the first inbound call doesn't pay the download cost
RUN uv run python -c "from pipecat.audio.vad.silero import SileroVADAnalyzer; SileroVADAnalyzer()"

# Copy app code last so source edits don't bust the deps layer
COPY src/ ./src/
COPY scripts/ ./scripts/

# FastAPI listens here; worker shim forwards traffic to this port
EXPOSE 8765
ENV HUD_WS_PORT=8765 \
    PYTHONUNBUFFERED=1

CMD ["uv", "run", "python", "-m", "src.hud_relay"]
