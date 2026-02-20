import json
import boto3
import urllib.request
import re

def get_secret(name):
    client = boto3.client("secretsmanager", region_name="us-east-1")
    return client.get_secret_value(SecretId=name)["SecretString"]

def normalize_phone(phone):
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"+1{digits}"
    elif len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return f"+{digits}"

def quo_request(endpoint, payload, api_key):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.openphone.com/v1/{endpoint}",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key
        },
        method="POST"
    )
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read())
        print(f"Quo response: {result}")
        return result

def get_phone_number_id(api_key):
    req = urllib.request.Request(
        "https://api.openphone.com/v1/phone-numbers",
        headers={"Authorization": api_key},
        method="GET"
    )
    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read())
        print(f"Phone numbers: {result}")
        return result["data"][0]["id"]

def add_contact(mobile, first_name, api_key):
    return quo_request("contacts", {
        "defaultFields": {
            "firstName": first_name,
            "phoneNumbers": [{"value": mobile}]
        }
    }, api_key)

def send_welcome(mobile, first_name, api_key, phone_number_id):
    message = f"Josh — RSVP Society has your number. We'll reach out when the time is right. — Jade"
    # personalize with actual name
    message = f"{first_name} — RSVP Society has your number. We'll reach out when the time is right. — Jade"
    return quo_request("messages", {
        "to": [mobile],
        "from": phone_number_id,
        "content": message
    }, api_key)

def handler(event, context):
    print(f"Event: {json.dumps(event)}")

    headers = {
        "Access-Control-Allow-Origin": "https://rsvpsociety.com",
        "Access-Control-Allow-Headers": "Content-Type",
        "Access-Control-Allow-Methods": "POST,OPTIONS"
    }

    if event.get("httpMethod") == "OPTIONS" or event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {"statusCode": 200, "headers": headers, "body": ""}

    try:
        body = json.loads(event.get("body", "{}"))
        print(f"Body: {body}")

        raw_phone = body.get("phone", "").strip()
        first_name = body.get("name", "").strip() or "Friend"

        if not raw_phone:
            return {"statusCode": 400, "headers": headers, "body": json.dumps({"error": "Phone required"})}

        mobile = normalize_phone(raw_phone)
        print(f"Mobile: {mobile}, Name: {first_name}")

        api_key = get_secret("rsvp/quo-api-key")
        phone_number_id = get_phone_number_id(api_key)

        add_contact(mobile, first_name, api_key)
        send_welcome(mobile, first_name, api_key, phone_number_id)

        return {"statusCode": 200, "headers": headers, "body": json.dumps({"status": "success"})}

    except Exception as e:
        print(f"EXCEPTION: {str(e)}")
        import traceback
        print(traceback.format_exc())
        return {"statusCode": 500, "headers": headers, "body": json.dumps({"error": str(e)})}
