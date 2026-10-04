"""Guest conversation handling, separate from the member's RSVP yes/no."""
import re
from event_policy import cutoff_reached
from sms_intent import _extract_plus_one_assignment, _looks_like_person_name, _looks_like_name_token, _is_question_like_text, UNKNOWN_PLUS_ONE_REPLIES
from sms_plus_one import remove_plus_one


def handle_guest(phone, text, normalized, invite, event, *, deps):
    if not invite:
        return None
    waiting = invite.get('awaitingPlusOneName') or invite.get('awaitingPlusOneLastName')
    assignment = _extract_plus_one_assignment(text)
    remove = bool(re.search(r"\b(remove|cancel|drop)\b.*(guest|plus\s*(one|1)|\+1)", text, re.I))
    solo = normalized in {'NO', 'NOPE', 'JUST ME', 'COMING ALONE', 'IM COMING ALONE', "I'M COMING ALONE", 'NO GUEST', 'NO PLUS ONE'}
    guest_intent = bool(re.search(r'(plus\s*(one|1)|\+1|my guest)', text, re.I))
    if not event.get('allowPlusOnes'):
        return "This one's just you." if assignment or guest_intent else None
    event_id = invite['eventId']
    if remove or (solo and waiting):
        if cutoff_reached(event, 12) and invite.get('plusOneName'):
            deps['request']('GUEST_REMOVE', 'Remove guest')
            return 'Let me see what I can do.'
        remove_plus_one(event_id, phone, deps=deps['plus_one_deps']())
        return "Got it. Just you this time. " + deps['confirmation_message'](phone)
    if waiting and normalized in UNKNOWN_PLUS_ONE_REPLIES:
        return "No worries. Send their name when you know. " + deps['confirmation_message'](phone)
    if not assignment and re.search(r"\b(cancel|cannot|can.t|won.t|not coming)\b", text, re.I):
        return None
    candidate = assignment
    if not candidate and not _is_question_like_text(text):
        if invite.get('awaitingPlusOneLastName') and _looks_like_name_token(text.strip()):
            candidate = invite['awaitingPlusOneLastName'] + ' ' + text.strip()
        elif _looks_like_person_name(text) and (waiting or invite.get('plusOneName')):
            candidate = text.strip()
        elif waiting and len(text.split()) == 1 and _looks_like_name_token(text) and normalized not in {'YES', 'YEAH', 'OK', 'OKAY', 'THANKS', 'SURE'}:
            deps['invites_table']().update_item(Key={'eventId': event_id, 'phone': phone},
                UpdateExpression='SET awaitingPlusOneLastName = :name REMOVE awaitingPlusOneName',
                ExpressionAttributeValues={':name': text.strip().title()})
            return 'And their last name?'
    if not candidate:
        if guest_intent and (not _is_question_like_text(text) or re.search(r'\b(change|update|replace|add)\b', text, re.I)):
            deps['set_awaiting'](event_id, phone)
            return 'Send their first and last name.'
        return None
    if not _looks_like_person_name(candidate):
        return 'Send their first and last name.'
    name = ' '.join(candidate.split()).title()[:100]
    valid, reply, is_member = deps['validate_candidate'](name, event_id, phone)
    if not valid:
        if reply == 'Let me check on that.' or 'more than one person' in reply:
            deps['request']('HANDOFF', 'Guest identity: ' + name)
        return reply
    if cutoff_reached(event, 12):
        deps['request']('GUEST_CHANGE', name)
        return 'The list is closed, but let me see if I can get the name changed.'
    result = deps['set_plus_one'](event_id, phone, name, is_member=is_member)
    if result == 'SAVED':
        return f'{name}. Got it. ' + (deps['confirmation_message'](phone) if waiting else '')
    if result == 'FULL':
        deps['request']('GUEST_CHANGE', name)
        return 'Let me see if I can get them added.'
    deps['request']('HANDOFF', 'Guest save needs review: ' + name)
    return "I couldn't add them yet. Let me check on that."
