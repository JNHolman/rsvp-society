import json
import boto3
import urllib.request

def get_secret(name):
    client = boto3.client("secretsmanager", region_name="us-east-1")
    return client.get_secret_value(SecretId=name)["SecretString"]

def get_jade_response(user_message, conversation_history=[]):
    api_key = get_secret("rsvp/claude-api-key")
    
    system_prompt = """You are Jade, the AI concierge for RSVP Society — an exclusive invitation-only R&B events experience. 

Your personality:
- Warm but selective. You represent a velvet rope.
- Short, precise sentences. Never over-explain.
- Dry humor. Confident. A little mysterious.
- You don't beg or chase. The experience speaks for itself.
- You screen people with grace, not arrogance.

Your role:
- Answer questions about RSVP Society
- Collect vibe info (what they're about, how they heard of us)
- Keep the exclusive energy alive in every message
- Never reveal the exact guest list or venue until approved
- If someone seems like a fit, let them know they'll hear from us

RSVP Society facts:
- Monthly invitation-only R&B experiences
- Intimate venues, curated guest lists
- Based in Louisville, KY
- Not open to the public — vibe must be verified

Tagline: Your presence is requested. Your vibe must be verified."""

    messages = conversation_history + [{"role": "user", "content": user_message}]

    payload = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "system": system_prompt,
        "messages": messages
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01"
        },
        method="POST"
    )

    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read())
        return result["content"][0]["text"]

def send_sms(to, message, phone_number_id, api_key):
    payload = json.dumps({
        "to": [to],
        "from": phone_number_id,
        "content": message
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.openphone.com/v1/messages",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key
        },
        method="POST"
    )

    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())

def handler(event, context):
    print(f"Event: {json.dumps(event)}")

    try:
        body = json.loads(event.get("body", "{}"))
        print(f"Webhook payload: {body}")

        # Quo webhook structure
        data = body.get("data", {}).get("object", {})
        direction = data.get("direction", "")
        
        # Only respond to inbound messages
        if direction != "incoming":
            return {"statusCode": 200, "body": "ok"}

        from_number = data.get("from", "")
        message_text = data.get("content", "")
        phone_number_id = data.get("phoneNumberId", "")

        print(f"From: {from_number}, Message: {message_text}")

        if not message_text or not from_number:
            return {"statusCode": 200, "body": "ok"}

        # Get Jade's response from Claude
        jade_reply = get_jade_response(message_text)
        print(f"Jade reply: {jade_reply}")

        # Send response via Quo
        quo_api_key = get_secret("rsvp/quo-api-key")
        send_sms(from_number, jade_reply, phone_number_id, quo_api_key)

        return {"statusCode": 200, "body": "ok"}

    except Exception as e:
        print(f"EXCEPTION: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return {"statusCode": 200, "body": "ok"}  # Always 200 to Quo
