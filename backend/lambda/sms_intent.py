import re

# ── Intent classification ─────────────────────────────────────────────────────

# ── Robust confirm/decline intent detection ──────────────────────────────────
# Exact set membership missed natural phrasing ("yes!", "Yes I'm in!", "count me in!",
# "can't make it"). RSVP state transitions are deterministic and must never leak to
# Claude, so detection has to catch how people actually text — punctuation-tolerant,
# phrase-tolerant — without being so loose it captures unrelated messages.

# Status-claim phrases ("I'm confirmed", "am I confirmed", "did I confirm") are NOT a
# fresh confirm — they are a question about state. Handled separately so they don't loop.
_STATUS_CLAIM_PHRASES = ("CONFIRMED", "AM I IN", "AM I CONFIRMED", "DID I CONFIRM", "MY STATUS")

def _is_status_question(normalized: str) -> bool:
    n = re.sub(r"[^A-Z' ]", "", normalized).strip()
    return any(p in n for p in _STATUS_CLAIM_PHRASES)

_CONFIRM_PHRASES = (
    "YES", "YEAH", "YEP", "YUP", "YEA", "SURE", "OK", "OKAY", "IN", "IM IN", "I'M IN",
    "COUNT ME IN", "ILL BE THERE", "I'LL BE THERE", "IM COMING", "I'M COMING",
    "COMING THROUGH", "ABSOLUTELY", "DEFINITELY", "OF COURSE", "SLIDING", "SLIDE",
    "FOR SURE", "BET", "DOWN", "IM DOWN", "I'M DOWN",
)
_DECLINE_PHRASES = (
    "NO", "NOPE", "NAH", "CANT", "CAN'T", "PASS", "DECLINE", "CANT MAKE IT",
    "CAN'T MAKE IT", "NOT COMING", "NOT GOING", "WONT MAKE IT", "WON'T MAKE IT",
    "IM OUT", "I'M OUT", "CANT GO", "CAN'T GO", "CANT DO IT", "CAN'T DO IT",
    "SOMETHING CAME UP", "MAYBE NEXT TIME", "I CANT COME", "I CAN'T COME", "I CANT MAKE IT", "I CAN'T MAKE IT",
    "CANCEL MY RSVP", "CANCEL MY RESERVATION", "PLEASE CANCEL MY RSVP",
)

def _is_rsvp_question(text: str, normalized: str) -> bool:
    raw = (text or "").strip()
    if "?" in raw:
        return True
    question_text = re.sub(r"[^A-Z' ]", " ", normalized or raw.upper())
    question_text = re.sub(r"\s+", " ", question_text).strip()
    question_text = re.sub(r"^(?:(?:OKAY|OK|SO|WELL|HEY) )+", "", question_text)
    return question_text.startswith((
        "IS ", "ARE ", "DO ", "DOES ", "DID ", "CAN ", "COULD ", "WOULD ",
        "WHAT ", "WHAT'S ", "WHATS ", "WHEN ", "WHERE ", "WHY ", "HOW ",
        "WHO ", "WHICH ", "WILL ", "SHOULD ", "AM ",
    ))


def _detect_rsvp_intent(text: str, normalized: str) -> str:
    """Return 'confirm', 'decline', or '' for a fresh RSVP action.
    Punctuation-tolerant, but conservative: only fires on short, RSVP-shaped messages
    so it never swallows a logistics question or a name."""
    if _is_rsvp_question(text, normalized):
        return ""
    n = re.sub(r"[^A-Z' ]", " ", normalized.replace("’", "'"))
    n = re.sub(r"\s+", " ", n).strip()
    if not n:
        return ""
    tokens = n.split()
    # Only treat short messages as RSVP actions.
    if len(tokens) > 5:
        return ""
    # Exact-message matching prevents short words such as IN, OK, SURE, PASS,
    # and NO from matching inside questions or unrelated sentences.
    if n in _DECLINE_PHRASES or n in {"NO THANKS", "NO THANK YOU"}:
        return "decline"
    if n in _CONFIRM_PHRASES or n in {
        "YES I'M IN", "YES IM IN", "YES I'LL BE THERE", "YES ILL BE THERE",
        "YES I'M COMING", "YES IM COMING",
    }:
        return "confirm"
    return ""


AMBIGUOUS_KEYWORDS = {
    "MAYBE", "MIGHT", "TRYING", "DEPENDS", "IDK", "I DON'T KNOW",
    "POSSIBLY", "HOPEFULLY", "WE'LL SEE", "NOT SURE", "PROBABLY",
    "I THINK SO", "SHOULD BE", "PLANNING ON IT",
}

# Messages Jade silently ignores — no response needed
IGNORE_KEYWORDS = {
    # Standard acks
    "THANKS", "THANK YOU", "THX", "TY", "APPRECIATE IT",
    "SOUNDS GOOD", "OK", "OKAY", "GOT IT", "COOL", "PERFECT",
    "GREAT", "AWESOME", "NICE", "SWEET",
    "AWESOME THANKS", "GREAT THANKS", "NICE THANKS", "SWEET THANKS",
    "AWESOME THANK YOU", "GREAT THANK YOU",
    # Slang closings
    "K", "KK", "BET", "BET BET", "FR", "FR FR", "WORD", "FACTS",
    "AIGHT", "AIIGHT", "ALRIGHT", "AITE", "ITE",
    "FS", "FOR SURE", "SAY LESS",
    "COPY", "NOTED", "WILL DO",
    # Multi-word combos
    "OK COOL", "OK GREAT", "OK THANKS", "OK PERFECT", "OK SOUNDS GOOD",
    "OK BET", "OK AIGHT", "OK FR", "OK K",
    "OKAY COOL", "OKAY GREAT", "OKAY THANKS", "OKAY PERFECT", "OKAY BET",
    "COOL THANKS", "COOL BET", "COOL FR",
    "GOT IT THANKS", "SOUNDS GOOD THANKS", "SOUNDS GREAT",
    "THAT WORKS", "THAT WORKS THANKS",
    "YEAH OK", "YEAH OKAY", "YEAH COOL", "YEAH BET", "YEAH FR",
    "YEP OK", "YEP COOL", "YEP BET",
    "LMAO", "LOL", "LMAO OK", "LOL OK",
    # Arrival — already there, no response needed
    "OMW", "ON MY WAY", "I'M OUTSIDE", "IM OUTSIDE", "OUTSIDE",
    "I'M HERE", "IM HERE", "HERE", "I'M THERE", "IM THERE",
    "JUST PULLED UP", "PULLED UP", "JUST GOT HERE", "JUST ARRIVED",
}

# Running late — acknowledge warmly, don't re-confirm
RUNNING_LATE_KEYWORDS = {
    "RUNNING LATE", "IM LATE", "I'M LATE", "GONNA BE LATE",
    "GOING TO BE LATE", "MIGHT BE LATE", "A LITTLE LATE",
    "RUNNING A LITTLE LATE", "BE THERE LATE", "STUCK IN TRAFFIC",
    "ON MY WAY BUT LATE",
}

# Cost / ticket questions — always deterministic, never Claude
COST_KEYWORDS = {
    "COST", "HOW MUCH", "PRICE", "TICKET", "TICKETS", "PAY",
    "IS IT FREE", "FREE", "CHARGE", "FEE", "COVER", "COVER CHARGE",
    "HOW MUCH IS IT", "WHAT DOES IT COST", "WHAT'S THE COST",
    "DO I NEED A TICKET", "DO I PAY", "IS THERE A COVER",
}

# Extra guest requests — firm one guest policy, never Claude
EXTRA_GUEST_KEYWORDS = {
    "CAN I BRING MORE", "CAN I BRING SOME MORE", "BRING MORE PEOPLE", "BRING SOME MORE",
    "MORE GUESTS", "CAN I BRING TWO", "BRING 2", "BRING TWO", "BRING 3", "BRING THREE",
    "MORE THAN ONE GUEST", "EXTRA GUEST", "EXTRA PEOPLE",
    "CAN MY FRIENDS COME", "CAN MY WHOLE CREW",
    "HOW MANY PEOPLE CAN I BRING", "HOW MANY CAN I BRING",
    "MORE PLUS ONES", "TWO PLUS ONES", "MULTIPLE GUESTS",
}

# Phrases that indicate a member wants to update their plus one —
# caught before general Jade so we can set state deterministically.
PLUS_ONE_UPDATE_INTENTS = {
    "UPDATE WHO I'M BRINGING", "UPDATE WHO IM BRINGING",
    "CHANGE WHO I'M BRINGING", "CHANGE WHO IM BRINGING",
    "UPDATE MY GUEST", "CHANGE MY GUEST", "SWITCH MY GUEST",
    "UPDATE MY PLUS ONE", "CHANGE MY PLUS ONE", "SWITCH MY PLUS ONE",
    "UPDATE MY PLUS 1", "CHANGE MY PLUS 1", "SWITCH MY PLUS 1",
    "CAN I UPDATE MY GUEST", "CAN I CHANGE MY GUEST",
    "CAN I UPDATE MY PLUS ONE", "CAN I CHANGE MY PLUS ONE",
    "CAN I UPDATE MY PLUS 1", "CAN I CHANGE MY PLUS 1",
    "CAN I UPDATE WHO I'M BRINGING", "CAN I UPDATE WHO IM BRINGING",
    "I WANT TO CHANGE MY GUEST", "I WANT TO UPDATE MY GUEST",
    "BRING SOMEONE ELSE", "DIFFERENT GUEST", "DIFFERENT PLUS ONE",
    "ACTUALLY MY GUEST IS", "MY GUEST IS NOW", "MY GUEST CHANGED",
}


_PLUS_ONE_ASSIGNMENT_PATTERNS = (
    re.compile(
        r"^(?:(?:(?:i['’]?m|i am)\s+(?:changing|updating|switching)|"
        r"(?:please\s+)?(?:change|update|switch|replace)|"
        r"(?:can|could)\s+i\s+(?:change|update|switch|replace)|"
        r"actually)\s+)?(?:my\s+)?(?:plus\s*(?:one|1)|guest|"
        r"who\s+(?:i['’]?m|i am)\s+bringing)\s+"
        r"(?:is|to|as|with|changed(?:\s+to)?)(?:\s+now)?\s+(.+)$",
        re.IGNORECASE,
    ),
)


def _extract_plus_one_assignment(text: str) -> str | None:
    """Return a guest name from a direct plus-one change, if one is present.

    Keep this deterministic so a confirmed member can change a guest in the
    same text instead of being sent through Jade's general conversation path.
    A bare change request intentionally returns None and uses the name prompt.
    """
    raw = (text or "").strip()
    for pattern in _PLUS_ONE_ASSIGNMENT_PATTERNS:
        match = pattern.match(raw)
        if match:
            name = match.group(1).strip().strip(".,!?;:")
            return name or None
    return None

OPT_OUT_KEYWORDS = {"STOP", "STOPALL", "STOP ALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}


def _is_opt_out_message(text: str) -> bool:
    """Match a whole opt-out keyword despite punctuation/case variations."""
    normalized = re.sub(r"[^A-Z0-9]+", " ", (text or "").upper()).strip()
    return normalized in OPT_OUT_KEYWORDS or normalized in {
        "PLEASE STOP", "STOP TEXTING ME", "PLEASE STOP TEXTING ME",
        "STOP SENDING ME TEXTS", "PLEASE STOP SENDING ME TEXTS",
        "UNSUBSCRIBE ME", "PLEASE UNSUBSCRIBE ME",
    }

QUESTION_LIKE_PLUS_ONE_TERMS = {
    "FOOD", "DRINK", "DRINKS", "BAR", "HOOKAH", "TIME", "WHERE", "WHEN",
    "PARK", "PARKING", "COST", "PRICE", "TICKET", "TICKETS", "COVER",
    "ADDRESS", "VENUE", "LOCATION", "START", "END", "ENDS", "DRESS", "WEAR",
}

UNKNOWN_PLUS_ONE_REPLIES = {
    "IDK", "I DON'T KNOW", "I DONT KNOW", "NOT SURE", "NO IDEA",
    "IDK YET", "I DON'T KNOW YET", "I DONT KNOW YET",
    "NOT SURE YET", "NOT YET", "IDK TBH", "UNSURE",
    "NO ONE YET", "NOBODY YET", "HAVEN'T DECIDED",
    "HAVENT DECIDED", "SKIP", "NONE",
    "I'M NOT SURE", "IM NOT SURE", "I'M UNSURE", "IM UNSURE",
    "NOT REALLY SURE", "I'M REALLY NOT SURE", "IM REALLY NOT SURE",
    "STILL NOT SURE", "STILL UNSURE", "STILL DECIDING",
    "HAVEN'T DECIDED YET", "HAVENT DECIDED YET",
}

CORRECTION_PHRASES = {
    "YOU'RE HALLUCINATING", "YOURE HALLUCINATING", "YOUR HALLUCINATING",
    "YOU ARE HALLUCINATING", "JADE YOU ARE HALLUCINATING", "JADE YOU'RE HALLUCINATING",
    "THAT'S WRONG", "THATS WRONG", "YOU'RE WRONG", "YOURE WRONG", "YOU ARE WRONG",
    "THAT IS WRONG", "THAT'S NOT RIGHT", "THATS NOT RIGHT", "THAT IS NOT RIGHT",
    "THAT'S INCORRECT", "THATS INCORRECT", "THAT IS INCORRECT", "NO THAT'S WRONG",
    "NO THATS WRONG", "NO THAT'S NOT RIGHT", "NO THATS NOT RIGHT",
    "YOU MADE THAT UP", "YOU'RE MAKING THAT UP", "YOURE MAKING THAT UP",
}

def _is_correction_text(text: str, normalized: str = "") -> bool:
    norm = normalized or (text or "").upper().strip()
    norm = re.sub(r"\s+", " ", norm).strip()
    return any(phrase in norm for phrase in CORRECTION_PHRASES)

def _correction_reply() -> str:
    return "You're right — I'll stick to confirmed event details. What do you want to know?"

def _normalized_tokens(value: str) -> set[str]:
    return set(re.findall(r"[A-Z0-9']+", (value or "").upper()))

def _contains_topic(tokens: set[str], *terms: str) -> bool:
    return bool(tokens & set(terms))

def _is_question_like_text(text: str, normalized: str = "") -> bool:
    raw = (text or "").strip()
    norm = normalized or raw.upper().strip()
    if not raw:
        return False
    if "?" in raw:
        return True
    question_starts = (
        "IS ", "ARE ", "DO ", "DOES ", "DID ", "CAN ", "COULD ", "WOULD ",
        "WHAT ", "WHAT'S ", "WHATS ", "WHEN ", "WHERE ", "WHY ", "HOW ",
        "WHO ", "WHICH ", "ANY ", "WILL ", "SHOULD ", "AM ",
    )
    # People often prepend an acknowledgment to a question. Strip common
    # lead-ins before testing for the first question word ("OK, what's...").
    question_text = re.sub(r"^[,\s]*(?:(?:OKAY|OK|SO|WELL|HEY)[,\s]+)+", "", norm)
    if question_text.startswith(question_starts):
        return True
    # Whole-word topic matching only. Do not substring-match names like Parker or Bartholomew.
    return bool(_normalized_tokens(norm) & QUESTION_LIKE_PLUS_ONE_TERMS)

def _looks_like_name_token(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or _is_question_like_text(raw):
        return False
    if raw.upper().strip() in UNKNOWN_PLUS_ONE_REPLIES:
        return False
    clean = re.sub(r"[^A-Za-z'’-]", "", raw)
    # Non-name words: acknowledgments, fillers, corrections, questions, logistics.
    # A real first/last name token should not be any of these.
    bad = {
        "IDK", "NOT", "SURE", "NONE", "NOBODY", "NO", "FOOD", "DRINKS", "HOOKAH",
        "QUESTION", "MAYBE", "LATER", "OK", "OKAY", "OKAYY", "OKK", "YES", "YEAH",
        "YEA", "YEP", "YUP", "NAH", "NOPE", "THANKS", "THANK", "THX", "TY", "COOL",
        "NICE", "GREAT", "BET", "FINE", "WAIT", "HOLD", "STOP", "WHO", "WHAT",
        "WHEN", "WHERE", "WHY", "HOW", "YOUR", "YOURE", "YOU", "ME", "MY", "MINE",
        "US", "WE", "THEY", "THEM", "LOL", "LMAO", "LMFAO", "HMM", "HMMM", "UM",
        "UH", "WTF", "OMG", "HELLO", "HEY", "HI", "HELP", "SOON", "NOW", "TODAY",
        "TONIGHT", "TOMORROW", "DUNNO", "NVM", "NEVERMIND", "WRONG", "LYING",
        "LIAR", "HALLUCINATING", "FAKE", "REAL", "NOTHING", "ANYTHING",
        "SOMETHING", "GOOD", "BAD", "DONE", "READY", "HERE", "THERE", "PLEASE",
        "ACTUALLY", "REALLY", "JUST", "STILL", "AND", "THE", "FOR", "WITH",
    }
    if len(clean) < 2 or clean.upper() in bad:
        return False
    return bool(re.match(r"^[A-Za-z][A-Za-z'’-]+$", clean))

_NAME_CATCH_STOPWORDS = {
    "WANNA", "WANT", "HANG", "FREE", "SINGLE", "HANGOUT", "CHILL", "LINK", "DATE",
    "MEET", "CALL", "TEXT", "COME", "THROUGH", "PULL", "UP", "OUT", "OVER", "SEE",
    "TALK", "KNOW", "LOVE", "LIKE", "MISS", "NEED", "GET", "GO", "DO", "BE", "AM",
    "ARE", "IS", "CAN", "WILL", "WOULD", "SHOULD", "COULD", "LETS", "LET",
}

def _is_bare_name_for_catch(text: str, normalized: str) -> bool:
    """Strict: 1-3 tokens that all look like name tokens AND contain no verb/rope/
    question/RSVP signal. Used only to deflect a name sent with no awaiting context;
    deliberately conservative so rope tests ('wanna hang') and questions never match."""
    raw = (text or "").strip()
    if not raw or _is_question_like_text(raw, normalized):
        return False
    parts = [p for p in re.split(r"\s+", raw) if p]
    if not (1 <= len(parts) <= 3):
        return False
    # Any stopword present -> not a name (kills "wanna hang", "are you free", etc.)
    up = re.sub(r"[^A-Z ]", " ", normalized).split()
    if any(w in _NAME_CATCH_STOPWORDS for w in up):
        return False
    return all(_looks_like_name_token(part) for part in parts)


def _looks_like_person_name(text: str) -> bool:
    raw = (text or "").strip()
    if not raw or _is_question_like_text(raw):
        return False
    parts = [p for p in re.split(r"\s+", raw) if p]
    if len(parts) < 2 or len(parts) > 3:
        return False
    excluded = _NAME_CATCH_STOPWORDS | {"TERRIBLE", "BAD", "DAY", "LAWN", "WORK", "THANK", "THANKS", "YOU", "GREAT", "TIME", "CONFIRMED", "ALREADY", "NO", "YES", "CANCEL", "MY"}
    if any(part.upper().strip(".,!?") in excluded for part in parts):
        return False
    return all(_looks_like_name_token(part) for part in parts)
