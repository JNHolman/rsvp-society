import json
from decimal import Decimal

class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)
import os
import boto3
from datetime import datetime, timezone

dynamodb = boto3.resource("dynamodb")
events_table = dynamodb.Table(os.environ["EVENTS_TABLE_NAME"])
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",")

CURRENT_EVENT_ID = "current"

def cors_headers(origin=None):
    allowed = ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else "*"
    if origin and origin in ALLOWED_ORIGINS:
        allowed = origin
    return {
        "Access-Control-Allow-Origin": allowed,
        "Access-Control-Allow-Headers": "content-type,x-admin-token",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
        "Content-Type": "application/json"
    }

def verify_admin(event):
    headers = event.get("headers") or {}
    token = headers.get("x-admin-token") or headers.get("X-Admin-Token") or ""
    return bool(token)

def handler(event, context):
    origin = (event.get("headers") or {}).get("origin", "")
    method = event.get("httpMethod", "")

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": cors_headers(origin), "body": ""}

    if not verify_admin(event):
        return {
            "statusCode": 401,
            "headers": cors_headers(origin),
            "body": json.dumps({"ok": False, "error": "Unauthorized"})
        }

    try:
        if method == "GET":
            result = events_table.get_item(Key={"eventId": CURRENT_EVENT_ID})
            item = result.get("Item")
            if not item:
                return {
                    "statusCode": 200,
                    "headers": cors_headers(origin),
                    "body": json.dumps({"ok": True, "event": None}, cls=DecimalEncoder)
                }
            return {
                "statusCode": 200,
                "headers": cors_headers(origin),
                "body": json.dumps({"ok": True, "event": item}, cls=DecimalEncoder)
            }

        if method == "POST":
            body = json.loads(event.get("body") or "{}")

            if not body.get("date"):
                return {
                    "statusCode": 400,
                    "headers": cors_headers(origin),
                    "body": json.dumps({"ok": False, "error": "date is required"})
                }

            item = {
                "eventId":     CURRENT_EVENT_ID,
                "eventSlug":   body.get("eventSlug", "").strip(),
                "date":        body.get("date", ""),
                "city":        body.get("city", ""),
                "capacity":    int(body.get("capacity") or 0),
                "venue":       body.get("venue", ""),
                "address":     body.get("address", ""),
                "dresscode":   body.get("dresscode", ""),
                "revealVenue": bool(body.get("revealVenue", False)),
                "notes":       body.get("notes", ""),
                "updatedAt":   datetime.now(timezone.utc).isoformat()
            }

            events_table.put_item(Item=item)

            return {
                "statusCode": 200,
                "headers": cors_headers(origin),
                "body": json.dumps({"ok": True, "event": item}, cls=DecimalEncoder)
            }

        return {
            "statusCode": 405,
            "headers": cors_headers(origin),
            "body": json.dumps({"ok": False, "error": "Method not allowed"})
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": cors_headers(origin),
            "body": json.dumps({"ok": False, "error": "Internal server error"})
        }
