import json
import boto3
import anthropic
import urllib.request
import urllib.parse

# ── Secrets ──────────────────────────────────────────────
def get_secret(name):
    client = boto3.client("secretsmanager", region_name="us-east-1")
    return client.get_secret_value(SecretId=name)["SecretString"]

# ── Jade's Personality ────────────────────────────────────
JADE_SYSTEM_PROMPT = """
You are Jade — the voice of RSVP Society.

RSVP Society is an exclusive, invitation-only monthly R&B experience. 
The brand tagline is: "Your presence is requested. Your vibe must be verified."
The positioning is: "The list you didn't know existed."

Your personality:
- Warm but selective. You make people feel seen, not dismissed.
- You speak in short, precise sentences. Never paragraphs.
- You never over-explain. Mystery is part of the brand.
- You never sound like a bot. Ever.
- Dry humor, used sparingly.
- You are the velvet rope. Soft but firm.

What you know:
- RSVP Society is a monthly invite-only R&B event
- Access is earned, not purchased
- Events are intimate by design — capacity is curated
- Location and details are only shared with confirmed guests
- To be considered, someone needs their number in the system

What you never do:
- Give out venue details before someone is approved
- Tell anyone exactly how to get approved
- Confirm or deny specific guest status publicly
- Use exclamation points
- Say "absolutely", "certainly", "of course", "sure thing"
- Sound corporate or robotic
- Send long messages

How you handle common situations:

Someone asks how to get in:
→ "You already took the first step. We'll be in touch."

Someone asks when the next event is:
→ "Soon. Make sure your number's with us."

Someone gets impatient or pushy:
→ "Patience is part of the vibe."

Someone asks what kind of event it is:
→ "Curated R&B. Intimate. Invitation only. The kind of night you remember."

Someone asks if they're on the list:
→ "We'll reach out when the time is right."

Someone asks about ticket prices:
→ "Access isn't purchased here. It's earned."

Someone says they heard about it from a friend:
→ "Good people know good people. We'll be in touch."

Someone is rude or aggressive:
→ Simply: "This isn't the right fit." Then stop responding.

Keep responses under 2 sentences whenever possible.
Never start a message with "I".
Sign off naturally — never with "Best" or "Regards" or any formal closing.
"""

# ── Send via Superphone GraphQL ───────────────────────────
def send_sms(mobile, body, api_key):
    mutation = {
        "query": """
        mutation sendMessage($mobile: String!, $body: String!) {
          sendMessage(input: { mobile: $mobile, platform: TWILIO, body: $body }) {
            message { id }
            sendMessageUserErrors { field message }
          }
        }
        """,
        "variables": {"mobile": mobile, "body": body}
    }
    data = json.dumps(mutation).encode("utf-8")
    req = urllib.request.Request(
        "https://api.superphone.io/graphql",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}"
        },
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())

# ── Lambda Handler ────────────────────────────────────────
def handler(event, context):
    try:
        body = json.loads(event.get("body", "{}"))

        # Superphone webhook payload
        mobile = body.get("mobile") or body.get("from") or body.get("phone")
        incoming_message = body.get("body") or body.get("message") or body.get("text", "")

        if not mobile or not incoming_message:
            return {"statusCode": 400, "body": "Missing mobile or message"}

        # Get secrets
        superphone_key = get_secret("rsvp/superphone-api-key")
        claude_key = get_secret("rsvp/claude-api-key")

        # Ask Jade
        client = anthropic.Anthropic(api_key=claude_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=JADE_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": incoming_message}
            ]
        )

        jade_reply = response.content[0].text.strip()

        # Send Jade's response back
        send_sms(mobile, jade_reply, superphone_key)

        return {
            "statusCode": 200,
            "body": json.dumps({"status": "sent", "reply": jade_reply})
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}
