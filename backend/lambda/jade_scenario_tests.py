#!/usr/bin/env python3
"""Jade scenario regression checks.

These are dependency-light guardrail tests for the cases that make Jade feel like
RSVP Society instead of a generic bot/promoter. They intentionally avoid live SMS
or DynamoDB so they can run in CI/static audit. Full end-to-end SMS tests should
still be run against a deployed test stack.
"""
from pathlib import Path
import os
import sys

os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import sms_handler as sms  # noqa: E402

SMS_SOURCE = "\n".join((HERE / name).read_text() for name in ("sms_handler.py", "jade_prompt.py", "sms_intent.py", "jade_service.py", "sms_guest_flow.py"))
JADE_PROMPT_SOURCE = (HERE / "jade_prompt.py").read_text()
MEMBER_STORE_SOURCE = (HERE / "member_store.py").read_text()


def require(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)


def test_identity_mystery() -> None:
    require("Jade prompt avoids bot denial", "You are not a bot" not in SMS_SOURCE)
    require("Jade does not discuss backend systems", "answer honestly and briefly" in SMS_SOURCE)
    require("Jade runtime prompt defines RSVP Society identity", "Jade from RSVP Society" in JADE_PROMPT_SOURCE)


def test_correction_guard() -> None:
    for phrase in ["You're hallucinating", "you made that up", "that's wrong"]:
        require(f"correction detected: {phrase}", sms._is_correction_text(phrase))
    require("correction reply is deterministic", sms._correction_reply() == "You're right — I'll stick to confirmed event details. What do you want to know?")


def test_multi_question_topic_detection() -> None:
    tokens = sms._normalized_tokens("Hookah? Sections?")
    require("hookah topic detected", sms._contains_topic(tokens, "HOOKAH"))
    require("sections topic detected", sms._contains_topic(tokens, "SECTION", "SECTIONS", "TABLE", "TABLES", "VIP", "BOOTH", "CABANA", "CABANAS"))


def test_unknown_logistics_are_safe() -> None:
    # v2: deflections are withholding-with-intent, not "posted yet" shrugs. Pin the
    # new lines and, more importantly, that NO banned "posted yet" phrasing is emitted.
    require("notes are interpreted in context", "needs_notes" in SMS_SOURCE)
    require("word presence cannot assert food availability", 'detail_parts.append("Food will be available.")' not in SMS_SOURCE)
    require("no 'posted yet' emitted in detail_parts",
            'detail_parts.append("Food info has not been posted yet.")' not in SMS_SOURCE)
    forbidden_defaults = [
        'detail_parts.append("Street parking is available.")',
        'detail_parts.append("Bar is open.")',
        'reply = "Street parking is available."',
        'reply = "Bar is open."',
    ]
    for pattern in forbidden_defaults:
        require(f"unsafe default absent: {pattern}", pattern not in SMS_SOURCE)


def test_plus_one_capture_safety() -> None:
    require("plus-one name branch asks for first and last", "Send their first and last name." in SMS_SOURCE)
    require("duplicate plus-one validation exists", "_validate_plus_one_candidate" in SMS_SOURCE)
    require("likely name guard exists", "_looks_like_person_name" in SMS_SOURCE)
    require("question-like plus-one guard exists", "_is_question_like_text" in SMS_SOURCE)


def test_private_event_gates() -> None:
    require("venue release policy is present in runtime prompt", "venue" in JADE_PROMPT_SOURCE.lower() and "release" in JADE_PROMPT_SOURCE.lower())
    require("ticket before confirmation blocked", "Once you're confirmed, I'll send what you need." in SMS_SOURCE)
    require("live only confirmable", "CONFIRMABLE_EVENT_STATES" in SMS_SOURCE and "CONFIRMABLE_EVENT_STATES = frozenset({\"LIVE\"})" in MEMBER_STORE_SOURCE)



def test_invited_unconfirmed_logistics_scrub() -> None:
    class FakeInvitesTable:
        def get_item(self, Key):
            return {"Item": {"status": "INVITED"}}

    old_event = sms._get_current_event
    old_invites = sms._invites_table
    try:
        sms._get_current_event = lambda: {
            "eventSlug": "gate-test",
            "event_status": "LIVE",
            "event_label": "Private Night",
            "date": "2099-12-31",
            "startTime": "21:00",
            "venue": "Secret Lounge",
            "address": "123 Hidden Way",
            "description": "Private room in the back",
            "jadeNotes": "Tell confirmed guests the side door code.",
            "sectionInfo": "VIP booth only",
            "parkingInfo": "Park behind the venue",
            "ticketUrl": "https://tickets.example.com",
            "revealVenue": True,
        }
        sms._invites_table = lambda: FakeInvitesTable()
        context = sms._build_event_context({"phone": "+15555550123"})
    finally:
        sms._get_current_event = old_event
        sms._invites_table = old_invites

    forbidden = [
        "venue_name:", "address_text:", "jade_notes:",
        "parking_info:", "ticket_url:",
        "Secret Lounge", "123 Hidden Way", "side door",
        "Park behind", "tickets.example.com",
    ]
    for token in forbidden:
        require(f"unconfirmed context strips {token}", token not in context)


def test_unconfirmed_operational_branch_is_gated() -> None:
    sent = []

    class FakeInvitesTable:
        def get_item(self, Key):
            return {"Item": {"status": "INVITED"}}

    old_env = dict(os.environ)
    old_event = sms._get_current_event
    old_invites = sms._invites_table
    old_member = sms.get_member
    old_send = sms.send_sms
    old_confirmed_invite = sms._get_confirmed_invite
    try:
        os.environ["ALLOW_UNSIGNED_WEBHOOK_DEV"] = "true"
        os.environ["SMS_ENABLED"] = "true"
        os.environ.pop("WEBHOOK_SECRET_ID", None)
        sms._get_current_event = lambda: {
            "eventSlug": "gate-test",
            "event_status": "LIVE",
            "event_label": "Private Night",
            "date": "2099-12-31",
            "startTime": "21:00",
            "jadeNotes": "Food from Las Mamas. Cash bar. No hookah.",
            "sectionInfo": "VIP booth only",
            "parkingInfo": "Park behind the venue",
            "ticketUrl": "https://tickets.example.com",
        }
        sms._invites_table = lambda: FakeInvitesTable()
        sms.get_member = lambda phone: {"phone": phone, "status": "APPROVED", "optOut": False, "smsOptIn": True}
        sms.send_sms = lambda phone, message: sent.append(message)
        sms._get_confirmed_invite = lambda phone: None

        event = {"body": '{"from":"+15555550123","body":"where do I park and is there food"}'}
        result = sms.handler(event, None)
    finally:
        sms._get_current_event = old_event
        sms._invites_table = old_invites
        sms.get_member = old_member
        sms.send_sms = old_send
        sms._get_confirmed_invite = old_confirmed_invite
        os.environ.clear()
        os.environ.update(old_env)

    require("handler returns 200", result.get("statusCode") == 200)
    require("unconfirmed operational question gets gate reply", sent == ["Once you're confirmed, I'll send what you need."])
    leaked = " ".join(sent)
    for token in ["Las Mamas", "Cash bar", "hookah", "VIP", "Park behind", "tickets.example.com"]:
        require(f"unconfirmed operational reply does not leak {token}", token not in leaked)


def test_confirmed_operational_branch_can_answer() -> None:
    sent = []

    class FakeInvitesTable:
        def get_item(self, Key):
            return {"Item": {"status": "CONFIRMED"}}

    old_env = dict(os.environ)
    old_event = sms._get_current_event
    old_invites = sms._invites_table
    old_member = sms.get_member
    old_send = sms.send_sms
    old_claude = sms._claude
    old_confirmed_invite = sms._get_confirmed_invite
    try:
        os.environ["ALLOW_UNSIGNED_WEBHOOK_DEV"] = "true"
        os.environ["SMS_ENABLED"] = "true"
        os.environ.pop("WEBHOOK_SECRET_ID", None)
        sms._get_current_event = lambda: {
            "eventSlug": "gate-test",
            "event_status": "LIVE",
            "event_label": "Private Night",
            "date": "2099-12-31",
            "startTime": "21:00",
            "jadeNotes": "Food from Las Mamas. Cash bar.",
            "parkingInfo": "Park behind the venue",
        }
        sms._invites_table = lambda: FakeInvitesTable()
        sms.get_member = lambda phone: {"phone": phone, "status": "APPROVED", "optOut": False, "smsOptIn": True}
        sms.send_sms = lambda phone, message: sent.append(message)
        sms._claude = lambda *args, **kwargs: "Park behind the venue. Food is from Las Mamas."
        sms._get_confirmed_invite = lambda phone: {"eventId": "gate-test", "phone": phone, "status": "CONFIRMED"}

        event = {"body": '{"from":"+15555550123","body":"where do I park and is there food"}'}
        result = sms.handler(event, None)
    finally:
        sms._get_current_event = old_event
        sms._invites_table = old_invites
        sms.get_member = old_member
        sms.send_sms = old_send
        sms._claude = old_claude
        sms._get_confirmed_invite = old_confirmed_invite
        os.environ.clear()
        os.environ.update(old_env)

    require("handler returns 200", result.get("statusCode") == 200)
    require("confirmed operational question receives logistics", sent and "Park behind the venue." in sent[0] and "Food is from Las Mamas." in sent[0])


def run() -> None:
    tests = [
        test_identity_mystery,
        test_correction_guard,
        test_multi_question_topic_detection,
        test_unknown_logistics_are_safe,
        test_plus_one_capture_safety,
        test_private_event_gates,
        test_invited_unconfirmed_logistics_scrub,
        test_unconfirmed_operational_branch_is_gated,
        test_confirmed_operational_branch_can_answer,
    ]
    failures = []
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures.append(f"{test.__name__}: {exc}")
    if failures:
        raise SystemExit("Jade scenario tests failed:\n- " + "\n- ".join(failures))
    print("jade scenario tests passed")


if __name__ == "__main__":
    run()
