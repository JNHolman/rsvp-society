
import re

with open('/Users/djinfamousone/Desktop/rsvp-society/backend/lambda/sms_handler.py', 'r') as f:
    content = f.read()

old = 'send_sms(from_phone, "You're confirmed. Details coming soon. See you there.")'
new = """# Get event date/address for confirmation
                    try:
                        import boto3 as _boto3
                        _ev_t = _boto3.resource("dynamodb").Table(os.getenv("EVENTS_TABLE_NAME", "rsvp-events"))
                        _ev = (_ev_t.get_item(Key={"eventId": "current"}).get("Item") or {})
                        _parts = ["You're in."]
                        if _ev.get("date"): _parts.append(f"See you {_ev['date']}.")
                        if _ev.get("revealVenue") and _ev.get("venue"): _parts.append(f"{_ev['venue']}.")
                        elif _ev.get("address"): _parts.append(f"{_ev['address']}.")
                        _parts.append("More details to follow.")
                        send_sms(from_phone, " ".join(_parts))
                    except Exception:
                        send_sms(from_phone, "You're in. See you there.")"""

content = content.replace(old, new)

old_no = 'send_sms(from_phone, "No worries — we'll catch you next time.")'
new_no = 'send_sms(from_phone, "No worries — we'll get you next time.")'
content = content.replace(old_no, new_no)

with open('/Users/djinfamousone/Desktop/rsvp-society/backend/lambda/sms_handler.py', 'w') as f:
    f.write(content)
print("Done")
