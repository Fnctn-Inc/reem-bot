"""Bind Twilio number → Reem TwiML App → reem.fnctn.io.

Uses a TwiML Application (voice_application_sid) instead of voice_url so the
shared Inca dev account's auto-overwriter can't reset it (it only touches
voice_url, which Twilio ignores when voice_application_sid is set).

Idempotent.
"""

from __future__ import annotations

import os
import sys

from dotenv import dotenv_values
from twilio.rest import Client

APP_FRIENDLY_NAME = "Reem FNOL"
APP_VOICE_URL = "https://reem.fnctn.io/twiml/inbound"


def main() -> int:
    e = dotenv_values(".env")
    needed = ("TWILIO_ACCOUNT_SID", "TWILIO_API_KEY_SID", "TWILIO_API_KEY_SECRET", "TWILIO_PHONE_NUMBER")
    for k in needed:
        if not e.get(k):
            print(f"missing env: {k}", file=sys.stderr)
            return 2

    client = Client(e["TWILIO_API_KEY_SID"], e["TWILIO_API_KEY_SECRET"], e["TWILIO_ACCOUNT_SID"])

    apps = [a for a in client.applications.list(limit=50) if a.friendly_name == APP_FRIENDLY_NAME]
    if apps:
        app = apps[0]
        if app.voice_url != APP_VOICE_URL or app.voice_method != "GET":
            client.applications(app.sid).update(voice_url=APP_VOICE_URL, voice_method="GET")
            print(f"updated app {app.sid}")
        else:
            print(f"reusing app {app.sid}")
    else:
        app = client.applications.create(
            friendly_name=APP_FRIENDLY_NAME,
            voice_url=APP_VOICE_URL,
            voice_method="GET",
        )
        print(f"created app {app.sid}")

    nums = client.incoming_phone_numbers.list(phone_number=e["TWILIO_PHONE_NUMBER"])
    if not nums:
        print(f"phone number {e['TWILIO_PHONE_NUMBER']} not in account", file=sys.stderr)
        return 3
    pn = nums[0]
    if pn.voice_application_sid != app.sid:
        client.incoming_phone_numbers(pn.sid).update(voice_application_sid=app.sid)
        print(f"bound number → app {app.sid}")
    else:
        print(f"number already bound to app {app.sid}")

    pn = client.incoming_phone_numbers(pn.sid).fetch()
    print()
    print(f"phone               {pn.phone_number}")
    print(f"voice_application   {pn.voice_application_sid}")
    print(f"voice_url (ignored) {pn.voice_url}")
    print(f"app voice_url       {app.voice_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
