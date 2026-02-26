import json
import os
import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource("dynamodb")
events_table = dynamodb.Table(os.environ["EVENTS_TABLE_NAME"])
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "").split(",")

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

def handler(event, context):
    origin = (event.get("headers") or {}).get("origin", "")
    method = event.get("httpMethod", "")

    if method == "OPTIONS":
        return {"statusCode": 200, "headers": cors_headers(origin), "body": ""}

    try:
        if method == "GET":
            result = events_table.scan()
            return {
                "statusCode": 200,
                "headers": cors_headers(origin),
                "body": json.dumps(result.get("Items", []))
            }

        if method == "POST":
            body = json.loads(event.get("body") or "{}")
            event_id = body.get("eventId")
            if not event_id:
                return {
                    "statusCode": 400,
                    "headers": cors_headers(origin),
                    "body": json.dumps({"error": "eventId required"})
                }
            events_table.put_item(Item=body)
            return {
                "statusCode": 201,
                "headers": cors_headers(origin),
                "body": json.dumps({"message": "Event created", "eventId": event_id})
            }

        if method == "PUT":
            body = json.loads(event.get("body") or "{}")
            event_id = body.get("eventId")
            if not event_id:
                return {
                    "statusCode": 400,
                    "headers": cors_headers(origin),
                    "body": json.dumps({"error": "eventId required"})
                }
            events_table.put_item(Item=body)
            return {
                "statusCode": 200,
                "headers": cors_headers(origin),
                "body": json.dumps({"message": "Event updated", "eventId": event_id})
            }

        if method == "DELETE":
            body = json.loads(event.get("body") or "{}")
            event_id = body.get("eventId")
            if not event_id:
                return {
                    "statusCode": 400,
                    "headers": cors_headers(origin),
                    "body": json.dumps({"error": "eventId required"})
                }
            events_table.delete_item(Key={"eventId": event_id})
            return {
                "statusCode": 200,
                "headers": cors_headers(origin),
                "body": json.dumps({"message": "Event deleted"})
            }

        return {
            "statusCode": 405,
            "headers": cors_headers(origin),
            "body": json.dumps({"error": "Method not allowed"})
        }

    except Exception as e:
        print(f"Error: {str(e)}")
        return {
            "statusCode": 500,
            "headers": cors_headers(origin),
            "body": json.dumps({"error": "Internal server error"})
        }
