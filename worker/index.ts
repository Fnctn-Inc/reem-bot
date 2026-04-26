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
  // The TwiML Application that all our calls flow through. The cron handler
  // re-binds this to our number whenever a shared-account peer wipes it.
  TWILIO_TWIML_APP_SID: string;
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

/**
 * Re-bind our Twilio number to our TwiML Application if a shared-account
 * peer has wiped it. Idempotent: if the binding is already correct we don't
 * touch it, and we ONLY query/modify our own phone number — other devs'
 * numbers on the shared account are never read or written.
 */
async function rebindTwilioIfNeeded(env: Env): Promise<string> {
  const auth = "Basic " + btoa(`${env.TWILIO_API_KEY_SID}:${env.TWILIO_API_KEY_SECRET}`);
  const headers = { Authorization: auth };
  const base = `https://api.twilio.com/2010-04-01/Accounts/${env.TWILIO_ACCOUNT_SID}`;

  // Server-side filter to ONLY return our number — we don't list anyone else's.
  const lookup = await fetch(
    `${base}/IncomingPhoneNumbers.json?PhoneNumber=${encodeURIComponent(env.TWILIO_PHONE_NUMBER)}`,
    { headers }
  );
  if (!lookup.ok) return `lookup failed ${lookup.status}`;
  const list = (await lookup.json()) as { incoming_phone_numbers?: Array<{ sid: string; voice_application_sid: string | null }> };
  const num = list.incoming_phone_numbers?.[0];
  if (!num) return `number ${env.TWILIO_PHONE_NUMBER} not found on this account`;

  if (num.voice_application_sid === env.TWILIO_TWIML_APP_SID) {
    return "already bound, no action";
  }

  const body = new URLSearchParams({ VoiceApplicationSid: env.TWILIO_TWIML_APP_SID });
  const update = await fetch(`${base}/IncomingPhoneNumbers/${num.sid}.json`, {
    method: "POST",
    headers: { ...headers, "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!update.ok) return `update failed ${update.status} ${await update.text()}`;
  return `rebound to ${env.TWILIO_TWIML_APP_SID}`;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    return getContainer(env.REEM, "singleton").fetch(request);
  },

  async scheduled(_event: ScheduledEvent, env: Env, ctx: ExecutionContext): Promise<void> {
    ctx.waitUntil(
      rebindTwilioIfNeeded(env).then(
        (msg) => console.log(`[twilio-rebind] ${msg}`),
        (err) => console.error(`[twilio-rebind] error: ${err}`)
      )
    );
  },
};
