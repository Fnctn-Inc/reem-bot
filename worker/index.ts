/**
 * Worker shim — front door for the Reem container.
 *
 * Receives every request to https://reem.fnctn.io and forwards it (HTTP or
 * WebSocket) into the Pipecat container running as a Container Durable Object.
 * One global instance ("singleton") is enough: a single FastAPI process can
 * happily serve all concurrent calls + the HUD socket.
 *
 * Worker secrets are NOT auto-passed to the container — we have to explicitly
 * surface them via `this.envVars` in the Container subclass constructor so
 * the Pipecat process can read them via os.getenv().
 */

import { Container, getContainer } from "@cloudflare/containers";

interface Env {
  REEM: DurableObjectNamespace<ReemContainer>;
  // Secrets — pushed via `wrangler secret put`. These appear on the Worker's
  // env, and we forward them into the container's process env.
  GRADIUM_API_KEY: string;
  GOOGLE_API_KEY: string;
  TAVILY_API_KEY: string;
  TWILIO_ACCOUNT_SID: string;
  TWILIO_API_KEY_SID: string;
  TWILIO_API_KEY_SECRET: string;
  TWILIO_PHONE_NUMBER: string;
  GRADIUM_VOICE_ID: string;
  PUBLIC_HOST: string;
}

export class ReemContainer extends Container<Env> {
  defaultPort = 8765;
  // Keep the container warm for 15 minutes after the last request — avoids
  // paying the Pipecat / silero / onnx import cost on every cold call.
  sleepAfter = "15m";

  // Allow direct outbound internet so Pipecat's long-lived WebSocket
  // connections to Gradium STT/TTS and Google Gemini stay open. With the
  // default Worker-proxy outbound mode, persistent WS connections get cut.
  enableInternet = true;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    // Forward Worker secrets into the container's process env so Pipecat
    // can read them via os.getenv().
    this.envVars = {
      GRADIUM_API_KEY: env.GRADIUM_API_KEY,
      GOOGLE_API_KEY: env.GOOGLE_API_KEY,
      TAVILY_API_KEY: env.TAVILY_API_KEY,
      TWILIO_ACCOUNT_SID: env.TWILIO_ACCOUNT_SID,
      TWILIO_API_KEY_SID: env.TWILIO_API_KEY_SID,
      TWILIO_API_KEY_SECRET: env.TWILIO_API_KEY_SECRET,
      TWILIO_PHONE_NUMBER: env.TWILIO_PHONE_NUMBER,
      GRADIUM_VOICE_ID: env.GRADIUM_VOICE_ID,
      PUBLIC_HOST: env.PUBLIC_HOST,
      HUD_WS_PORT: "8765",
    };
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    return getContainer(env.REEM, "singleton").fetch(request);
  },
};
