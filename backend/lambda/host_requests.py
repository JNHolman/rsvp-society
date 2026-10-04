"""Event exceptions use the existing host-only coded SMS approval channel."""
import hashlib
import json
import os
import secrets
from datetime import datetime, timezone, timedelta

import boto3
from botocore.exceptions import ClientError


def _table():
    return boto3.resource('dynamodb').Table(os.getenv('PENDING_APPROVALS_TABLE_NAME', 'rsvp-pending-approvals'))


def queue_request(event, phone, name, kind, detail, *, hosts, send_sms, current_guest=None):
    if not hosts:
        raise RuntimeError('No host is configured for this request')
    slug = event.get('eventSlug') or event.get('eventId')
    digest = hashlib.sha256(json.dumps([slug, phone, kind, detail], sort_keys=True).encode()).hexdigest()[:24]
    request_id = 'REQUEST#' + digest
    table = _table()
    key = {'hostPhone': 'REQUEST', 'memberPhone': request_id}
    row = table.get_item(Key=key, ConsistentRead=True).get('Item') or {}
    now = datetime.now(timezone.utc)
    if row.get('state') in {'PENDING', 'PROCESSING'} and int(row.get('expiresAt') or 0) > int(now.timestamp()):
        return row
    row = {**key, 'requestKind': kind, 'targetPhone': phone, 'memberName': name,
           'eventId': slug, 'detail': detail, 'state': 'PENDING',
           'approvalCode': f'{secrets.randbelow(1000000):06d}', 'createdAt': now.isoformat(),
           'expiresAt': int((now + timedelta(hours=24)).timestamp())}
    if current_guest is not None:
        row['expectedGuestName'] = current_guest
    try:
        table.put_item(Item=row, ConditionExpression='attribute_not_exists(memberPhone) OR #s IN (:done, :denied, :failed) OR expiresAt <= :now',
                      ExpressionAttributeNames={'#s': 'state'},
                      ExpressionAttributeValues={':done': 'DONE', ':denied': 'DENIED', ':failed': 'FAILED', ':now': int(now.timestamp())})
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return table.get_item(Key=key, ConsistentRead=True)['Item']
        raise
    alerted = 0
    try:
        for host in hosts:
            table.put_item(Item={**row, 'hostPhone': host})
            code = row['approvalCode']
            prompt = f'{kind.replace("_", " ").title()} — {name} ({phone}), {slug}: {detail}. '
            if kind != 'HANDOFF':
                prompt += f'Reply Y {code} or N {code}.'
            else:
                prompt += 'Contact this member to follow up.'
            send_sms(host, prompt)
            alerted += 1
        return row
    except Exception:
        if not alerted:
            table.update_item(Key=key, UpdateExpression='SET #s = :failed',
                              ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':failed': 'FAILED'})
        raise


def decide_request(pending, approve, *, deps):
    if pending.get('requestKind') == 'HANDOFF':
        return 'Contact this member to follow up.'
    table = _table()
    key = {'hostPhone': 'REQUEST', 'memberPhone': pending['memberPhone']}
    try:
        result = table.update_item(Key=key, UpdateExpression='SET #s = :processing',
            ConditionExpression='#s = :pending AND expiresAt > :now',
            ExpressionAttributeNames={'#s': 'state'},
            ExpressionAttributeValues={':processing': 'PROCESSING', ':pending': 'PENDING', ':now': int(datetime.now(timezone.utc).timestamp())},
            ReturnValues='ALL_NEW')
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return 'This request has already been handled or expired.'
        raise
    request = result['Attributes']
    phone = request['targetPhone']
    kind = request['requestKind']
    state = 'DENIED'
    try:
        event = deps['get_current_event']()
        if request['eventId'] != (event.get('eventSlug') or event.get('eventId')) or event.get('event_status') != 'LIVE':
            approve = False
        message = "I couldn't get it changed."
        if kind == 'WAITLIST':
            message = "I couldn't get you added this time."
        if approve and 'expectedGuestName' in request:
            current = deps['plus_one_deps']()['invites_table']().get_item(Key={'eventId': request['eventId'], 'phone': phone}, ConsistentRead=True).get('Item') or {}
            if str(current.get('plusOneName') or '') != request['expectedGuestName']:
                approve = False
                message = "That request is no longer current."
        if approve:
            if kind == 'GUEST_CHANGE':
                valid, reply, is_member = deps['validate_candidate'](request['detail'], request['eventId'], phone)
                if not valid:
                    message = reply
                elif deps['set_plus_one'](request['eventId'], phone, request['detail'], is_member, expected_name=request.get('expectedGuestName'), capacity_override=int(event.get('confirmedHeadcount') or 0) + 1) == 'SAVED':
                    state, message = 'DONE', f"Got it. {request['detail']} is added."
            elif kind == 'GUEST_REMOVE':
                from sms_plus_one import remove_plus_one
                if remove_plus_one(request['eventId'], phone, deps=deps['plus_one_deps']()) == 'SAVED':
                    state, message = 'DONE', "Got it. Just you this time."
            elif kind == 'CANCEL':
                if deps['cancel_invite'](request['eventId'], phone):
                    state, message = 'DONE', "No worries. Your RSVP is canceled."
            elif kind == 'WAITLIST':
                # The operator may approve an extra seat, but the same atomic RSVP
                # mutation still owns counters and duplicate protection.
                result = deps['confirm_invite'](request['eventId'], phone, int(event.get('confirmedHeadcount') or 0) + 1)
                if result == 'CONFIRMED':
                    state, message = 'DONE', deps['confirmation_message'](phone)
        table.update_item(Key=key, UpdateExpression='SET #s = :state, replyText = :reply',
                          ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':state': state, ':reply': message})
        member = deps['get_member'](phone)
        if member and member.get('status') == 'APPROVED' and not member.get('optOut') and member.get('smsOptIn') is not False:
            deps['send_sms'](phone, message)
        return 'Done.' if state == 'DONE' else 'Request not added. Member notified.'
    except Exception:
        table.update_item(Key=key, UpdateExpression='SET #s = :failed',
                          ExpressionAttributeNames={'#s': 'state'}, ExpressionAttributeValues={':failed': 'FAILED'})
        raise
