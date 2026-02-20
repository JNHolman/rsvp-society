import json
import boto3
import urllib.request
import urllib.parse
import re

# ── Secrets ──────────────────────────────────────────────
def get_secret(name):
    client = boto3.client("secretsmanager", region_name="us-east-1")
    return client.get_secret_value(SecretId=name)["SecretString"]

# ── Normalize phone to E.164 ──────────────────────────────
def normalize_phone(phone):
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"

# ── Superphone GraphQL ────────────────────────────────────
def superphone_request(query, variables, api_key):
    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        "https://api.superphone.io/graphql",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key}"
        },
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read())

def add_contact(mobile, first_name, api_key):
    mutation = """
    mutation createContact($mobile: String!, $firstName: String!) {
      createContact(input: { mobile: $mobile, firstName: $firstName }) {
        contact { id mobile }
        userErrors { field message }
      }
    }
    """
    return superphone_request(mutation, {"mobile": mobile, "firstName": first_name}, api_key)

def send_welcome(mobile, first_name, api_key):
    welcome = f"Hey {first_name} — your request is in. We'll be in touch if the vibe matches. — Jade"
    mutation = """
    mutation sendMessage($mobile: String!, $body: String!) {
      sendMessage(input: { mobile: $mobile, platform: TWILIO, body: $body }) {
        message { id }
        sendMessageUserErrors { field message }
      }
    }
    """
    return superphone_request(mutation, {"mobile": mobile, "body": welcome}, api_key)

# ── Lambda Handler ────────────────────────────────────────
def handler(event, context):
    # CORS headers
    headers = {
        "Access-Control-Allow-Origin": "https://rsvpsociety.com",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST,OPTIONS"
    }

    # Handle preflight
    if event.get("httpMethod") == "OPTIONS":
        return {"statusCode": 200, "headers": headers, "body": ""}

    try:
        body = json.loads(event.get("body", "{}"))
        raw_phone = body.get("phone", "").strip()
        first_name = body.get("name", "").strip() or "Guest"

        if not raw_phone:
            return {
                "statusCode": 400,
                "headers": headers,
                "body": json.dumps({"error": "Phone number required"})
            }

        mobile = normalize_phone(raw_phone)
        api_key = get_secret("rsvp/superphone-api-key")

        # Add to Superphone
        add_contact(mobile, first_name, api_key)

        # Send Jade's welcome SMS
        send_welcome(mobile, first_name, api_key)

        return {
            "statusCode": 200,
            "headers": headers,
            "body": json.dumps({"status": "success"})
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": headers,
            "body": json.dumps({"error": str(e)})
        }
