"""Point Inca's Twilio German DID at our Pipecat /twiml/inbound endpoint.

Idempotent: re-runnable. Strips any prior trunk attachment, sets voice_url
to PUBLIC_HOST/twiml/inbound (HTTP GET).

Run after PUBLIC_HOST is in .env. The Pipecat agent then handles every
inbound call via Twilio Media Streams.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from twilio.rest import Client


def main() -> int:
    load_dotenv()

    sid = os.getenv("TWILIO_ACCOUNT_SID")
    api_key = os.getenv("TWILIO_API_KEY_SID")
    api_secret = os.getenv("TWILIO_API_KEY_SECRET")
    number = os.getenv("TWILIO_PHONE_NUMBER")
    public_host = os.getenv(
        "PUBLIC_HOST", "throwing-fitted-nut-strange.trycloudflare.com"
    )

    for name, value in {
        "TWILIO_ACCOUNT_SID": sid,
        "TWILIO_API_KEY_SID": api_key,
        "TWILIO_API_KEY_SECRET": api_secret,
        "TWILIO_PHONE_NUMBER": number,
    }.items():
        if not value:
            print(f"missing env: {name}", file=sys.stderr)
            return 2

    voice_url = f"https://{public_host}/twiml/inbound"

    client = Client(api_key, api_secret, sid)

    numbers = client.incoming_phone_numbers.list(phone_number=number)
    if not numbers:
        print(f"phone number {number} not found in account", file=sys.stderr)
        return 3
    pn = numbers[0]

    # Detach from any trunk + set voice_url so Twilio fetches our TwiML.
    update_kwargs: dict = {"voice_url": voice_url, "voice_method": "GET"}
    if pn.trunk_sid:
        update_kwargs["trunk_sid"] = ""  # detach
    client.incoming_phone_numbers(pn.sid).update(**update_kwargs)

    pn = client.incoming_phone_numbers(pn.sid).fetch()
    print(f"  phone     {pn.phone_number}")
    print(f"  trunk_sid {pn.trunk_sid or '(none — detached)'}")
    print(f"  voice_url {pn.voice_url}")
    print()
    print(f"Anyone dialing {number} will now hit:")
    print(f"  {voice_url}")
    print("→ /twiml/inbound returns <Connect><Stream> → Pipecat /ws/twilio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
