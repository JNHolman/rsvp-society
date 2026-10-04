"""One-time follow-ups. Every send rechecks the current invite and consent."""
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from event_policy import event_start


def schedule(event, kind, identity, *, after_hours, now=None):
    arn = os.getenv('REMINDER_LAMBDA_ARN')
    role = os.getenv('REMINDER_SCHEDULER_ROLE_ARN')
    if not arn or not role:
        return False
    slug = event.get('eventSlug') or event.get('eventId')
    fire = (now or datetime.now(timezone.utc)) + timedelta(hours=after_hours)
    if fire >= event_start(event).astimezone(timezone.utc) - timedelta(hours=12):
        return False
    digest = hashlib.sha256(f'{slug}:{kind}:{identity}'.encode()).hexdigest()[:24]
    try:
        boto3.client('scheduler').create_schedule(
            Name='rsvp-followup-' + digest,
            ScheduleExpression=f'at({fire.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")})',
            ScheduleExpressionTimezone='UTC', FlexibleTimeWindow={'Mode': 'OFF'}, ActionAfterCompletion='DELETE',
            Target={'Arn': arn, 'RoleArn': role,
                    'Input': json.dumps({'source': 'rsvp.followup', 'kind': kind, 'identity': str(identity), 'eventId': slug}),
                    'RetryPolicy': {'MaximumEventAgeInSeconds': 3600, 'MaximumRetryAttempts': 2}})
        return True
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConflictException':
            return True
        raise


def handle(payload, *, send_sms):
    db = boto3.resource('dynamodb')
    events = db.Table(os.getenv('EVENTS_TABLE_NAME', 'rsvp-events'))
    invites = db.Table(os.getenv('INVITES_TABLE_NAME', 'rsvp-event-invites'))
    members = db.Table(os.getenv('MEMBERS_TABLE_NAME', 'rsvp-members'))
    event_id = payload['eventId']
    event = events.get_item(Key={'eventId': event_id}, ConsistentRead=True).get('Item') or {}
    pointer = events.get_item(Key={'eventId': 'current'}, ConsistentRead=True).get('Item') or {}
    active = pointer.get('activeEventSlug') or pointer.get('eventSlug')
    if active != event_id or event.get('event_status') != 'LIVE' or datetime.now(timezone.utc) >= event_start(event):
        return {'ok': True, 'skipped': True}
    kind = payload['kind']
    if kind not in {'wave', 'guest', 'guest_release'}:
        raise ValueError('Unknown follow-up kind')
    if os.getenv('SMS_ENABLED', 'false').lower() != 'true' and kind != 'guest_release':
        return {'ok': True, 'skipped': True, 'reason': 'SMS disabled'}
    if kind == 'wave':
        rows = []
        query = {'KeyConditionExpression': Key('eventId').eq(event_id)}
        while True:
            page = invites.query(**query)
            rows.extend(row for row in page.get('Items', []) if str(row.get('waveNumber')) == payload['identity'])
            if not page.get('LastEvaluatedKey'):
                break
            query['ExclusiveStartKey'] = page['LastEvaluatedKey']
    else:
        row = invites.get_item(Key={'eventId': event_id, 'phone': payload['identity']}, ConsistentRead=True).get('Item')
        rows = [row] if row else []
    if payload.get('remainingPhones') is not None:
        remaining = set(payload['remainingPhones'])
        rows = [row for row in rows if row['phone'] in remaining]
    sent = 0
    for index, row in enumerate(rows):
        if sent >= 15:
            function = os.getenv('AWS_LAMBDA_FUNCTION_NAME')
            if not function:
                raise RuntimeError('Follow-up continuation is not configured')
            queued = {**payload, 'remainingPhones': [item['phone'] for item in rows[index:]]}
            result = boto3.client('lambda').invoke(FunctionName=function, InvocationType='Event', Payload=json.dumps(queued).encode())
            if result.get('StatusCode') != 202:
                raise RuntimeError('Follow-up continuation was not accepted')
            break
        phone = row['phone']
        if kind == 'guest_release':
            # An unnamed place is a pending intent, not an additional occupied seat.
            try:
                invites.update_item(Key={'eventId': event_id, 'phone': phone},
                    UpdateExpression='SET guestPlaceholderReleased = :true REMOVE awaitingPlusOneName, awaitingPlusOneLastName',
                    ConditionExpression='#s = :confirmed AND attribute_exists(guestFollowupClaim) AND (attribute_not_exists(plusOneName) OR plusOneName = :empty)',
                    ExpressionAttributeNames={'#s': 'status'},
                    ExpressionAttributeValues={':true': True, ':confirmed': 'CONFIRMED', ':empty': ''})
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
            continue
        status = 'INVITED' if kind == 'wave' else 'CONFIRMED'
        if (kind == 'wave' and row.get('waitlisted')) or row.get('status') != status or (kind == 'guest' and (row.get('plusOneName') or not row.get('awaitingPlusOneName'))):
            continue
        member = members.get_item(Key={'phone': phone}, ConsistentRead=True).get('Item') or {}
        if member.get('status') != 'APPROVED' or member.get('optOut') or member.get('smsOptIn') is False:
            continue
        field = 'responseFollowupClaim' if kind == 'wave' else 'guestFollowupClaim'
        condition = '#s = :status AND attribute_not_exists(#claim)'
        if kind == 'wave':
            condition += ' AND attribute_not_exists(waitlisted)'
        values = {':status': status, ':now': datetime.now(timezone.utc).isoformat()}
        if kind == 'guest':
            condition += ' AND (attribute_not_exists(plusOneName) OR plusOneName = :empty)'
            values[':empty'] = ''
        try:
            invites.update_item(Key={'eventId': event_id, 'phone': phone},
                UpdateExpression='SET #claim = :now', ConditionExpression=condition,
                ExpressionAttributeNames={'#s': 'status', '#claim': field}, ExpressionAttributeValues=values)
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                continue
            raise
        text = (f"Still coming to {event.get('event_label') or 'the event'}?" if kind == 'wave'
                else "Still bringing someone? Send their first and last name by tomorrow. Otherwise I'll leave it as just you.")
        # Keep the claim on an uncertain provider result: automatic retries must
        # not duplicate an SMS that the provider may already have accepted.
        if kind == 'guest':
            schedule(event, 'guest_release', phone, after_hours=24)
        send_sms(phone, text)
        sent += 1
    return {'ok': True, 'sent': sent}
