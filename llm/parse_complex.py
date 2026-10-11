"""Finance questions: income changes, bonuses, several items, loans and mortgages.

Gemini first (`parse_with_gemini`), regex grammar as the offline fallback. Both produce the same small
`spec` dict of raw numbers *as written in the text*; code (never the LLM)
turns the spec into cash-flow items and computes loan payments. Every dollar
amount, percentage and loan term in the text must be accounted for by the spec
(and vice versa), otherwise the question is refused rather than guessed.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter
from datetime import date

from core.housing import is_home_loan
from core.income import NET_FACTOR
from core.rates import DEFAULT_RATES
from core.loans import DAYS_PER_MONTH, loan_decision
from core.model import Decision, DecisionDelta

MAX_START_DAY = 730
DAYS_PER_YEAR_MONTHLY = 365 / 12  # an annual salary paid monthly

HINT = ("I couldn't tell how every number fits in. Try one clear sentence, for example: "
        "\"Can I afford a $350k house with 20% down at 7% over 30 years?\" or "
        "\"I got a $500/month raise, can I afford $1,200/month rent?\"")

INCOME_CUES = ("make", "making", "made", "earn", "salary", "raise", "income", "job", "wage", "promotion",
               "bonus", "hired", "offer", "freelance", "gig", "received", "inherit", "gift", "refund", "prize",
               "payout", "stimulus", "settlement", "windfall")
# Income with no stated period that arrives once (a bonus, a gift), not every month.
ONE_TIME_INCOME_CUES = ("bonus", "gift", "refund", "inherit", "prize", "payout", "stimulus", "settlement",
                        "windfall", "made", "received", "earned", "won", "sold")
# A bare number right after one of these is an amount ("spend 300"), never a product name.
AMOUNT_WORDS = {"spend", "spending", "spent", "buy", "buying", "pay", "paying", "paid", "cost", "costs", "for",
                "of", "about", "around", "is", "was", "at", "afford", "make", "made", "making", "earn", "get", "got",
                "borrow", "owe", "save", "with", "and", "or", "plus", "extra", "only", "just", "maybe", "like",
                "take", "rent", "loan", "salary", "bonus", "raise", "down", "over", "under", "to", "by", "worth"}
PRONOUNS = {"it", "this", "that", "them", "these", "those", "one", "something", "stuff", "thing"}
PRODUCT_SUFFIXES = ("pro", "max", "ultra", "plus", "mini", "air", "series", "se", "lite", "edition", "gen")
PURCHASE_CUES = ("house", "home", "condo", "mortgage", "loan", "car", "borrow", "financ", "financed",
                 "financing", "lease")
NET_WORDS = ("take-home", "take home", "after tax", "after-tax", "net ", "deposit")
REPLACE_CUES = (" make ", "new job", "got a job", "got the job", "job making", "job paying", "salary", "promotion",
                "hired", "offer", "making", "earning")
ADDITIVE_CUES = ("raise", "bonus", "freelance", "gig", "extra", "additional")

MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"])}

_TOKEN = re.compile(
    r"(?P<dollar>\$)?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(?P<k>[kK](?![a-zA-Z]))?"
    r"(?P<pct>\s?%|\s?percent\b)?"
    r"(?:\s?-?\s?(?P<unit>years?|yrs?|months?|mos?|days?|weeks?|wks?)\b)?"
    r"(?P<ord>st|nd|rd|th)?(?!\w)")


_NUM_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen".split())}
_NUM_WORDS.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
                   "ninety": 90})
_ONES = "|".join(w for w in _NUM_WORDS if 0 < _NUM_WORDS[w] < 10)
_NUM_WORD = "|".join(sorted(_NUM_WORDS, key=len, reverse=True))
_SPAN = r"(?=\s*(?:years?|yrs?|months?|mos?|weeks?|wks?|days?|grand|k\b|thousand|million)\b)"


def normalize_text(text: str) -> str:
    """Spell numbers the way the tokenizer reads them: "six months" -> "6 months", "30k", "3 grand" -> "3000".

    Done once at the entrance so Gemini, the regex grammar and the coverage check all see the same digits.
    The decision keeps the user's original wording as its raw_text.
    """
    def word_num(m):
        tens, ones = m.group(1).lower(), (m.group(2) or "").lower()
        return str(_NUM_WORDS[tens] + (_NUM_WORDS.get(ones, 0) if ones else 0))
    text = re.sub(rf"\b({_NUM_WORD})(?:[- ]({_ONES}))?\b{_SPAN}", word_num, text, flags=re.I)
    text = re.sub(r"\b(over|for|within|in|after|into)\s+a\s+(year|month|week)\b", r"\1 1 \2", text, flags=re.I)
    text = re.sub(r"\b(a|1)\s+(year|month|week)\s+(from now|from today|later)\b", r"1 \2 \3", text, flags=re.I)

    def scaled(m):
        value = float(m.group(2).replace(",", "")) * (1_000_000 if m.group(3).lower() == "million" else 1000)
        return f"{m.group(1) or ''}{value:.0f}"
    return re.sub(r"(\$\s?)?(\d+(?:,\d{3})*(?:\.\d+)?)\s*(grand|thousand|million)\b", scaled, text, flags=re.I)


class LoanTermMissing(ValueError):
    """A loan we can't guess the length of (anything but a home). The message asks the person for it."""

    def __init__(self, label: str, price: float | None = None, financed: float | None = None):
        thing = label.lower() if label and label.lower() not in ("loan", "expense") else "it"
        amount = f"${financed:,.0f} " if financed else ""
        example = (f'"...a {thing} for ${price:,.0f}, financing ${financed:,.0f} over 2 years"'
                   if price and financed else f'"...a {thing} loan over 2 years"')
        super().__init__(f"How long would you take to pay off the {amount}loan for the {thing}? "
                         f"Tell me the term, for example: {example}.")


class ComplexParseError(ValueError):
    """Regex grammar couldn't account for the text; try the LLM fallback."""


def _words(s: str) -> list[str]:
    return re.findall(r"[a-z'$/%-]+", s.lower())


def tokenize(text: str):
    """Numeric tokens with kind: money | pct | term | timing | date."""
    toks = []
    for m in _TOKEN.finditer(text):
        raw = m["num"].replace(",", "")
        try:
            val = float(raw)
        except ValueError:
            continue
        if m["k"]:
            val *= 1000
        before = text[:m.start()].lower()
        after = text[m.end():].lower()
        tok = {"value": val, "start": m.start(), "end": m.end(), "dollar": bool(m["dollar"]), "text": m[0]}
        unit = (m["unit"] or "").lower()
        if m["pct"]:
            tok["kind"] = "pct"
        elif m["ord"]:
            tok["kind"] = "date"
        elif unit:
            u = ("year" if unit.startswith(("y",)) else "month" if unit.startswith("mo")
                 else "week" if unit.startswith("w") else "day")
            tok["unit"] = u
            # "in 3 days" delays the start; every other duration is a loan/payment term.
            tok["kind"] = ("timing" if re.search(r"\b(in|after|within)(?:\s+(?:about|around|roughly|like|maybe|just|only|another))?\s*$", before)
                           or re.match(r"\s*(from now|from today|later|out)\b", after) else "term")
            if u in ("day", "week") and tok["kind"] != "timing":
                tok["kind"] = "term"  # unsupported unit; regex_spec refuses it
        elif re.search(r"\b(" + "|".join(MONTHS) + r")\s*$", before) and val <= 31 and not m["dollar"]:
            tok["kind"] = "date"
        elif _is_name_number(text, m, before, after):
            tok["kind"] = "name"  # "iPhone 18", "PS5", "Galaxy S24": part of a name, not an amount
        else:
            tok["kind"] = "money"
        # unit-less "/mo", "a year" cadences are read from `after` by the caller.
        tok["after"] = after
        tok["before"] = before
        toks.append(tok)
    return toks


def _is_name_number(text: str, m, before: str, after: str) -> bool:
    """A bare number that's part of a product or model name rather than a dollar amount."""
    if m["dollar"] or m["k"] or m["pct"] or m["unit"] or m["ord"]:
        return False
    if re.match(r"\s*(dollars?|bucks?|usd)\b", after) or _cadence_safe(after):
        return False
    start = m.start("num")
    if start > 0 and text[start - 1].isalpha():  # glued to letters: PS5, S24
        return True
    prev = re.search(r"([A-Za-z][A-Za-z'-]*)\s+$", text[:start])
    if not prev or prev.group(1).lower() in AMOUNT_WORDS:
        return False
    word = prev.group(1)
    nxt = re.match(r"\s*([A-Za-z]+)", after)
    product_tail = bool(nxt) and nxt.group(1).lower() in PRODUCT_SUFFIXES
    camel = any(c.isupper() for c in word[1:]) or (word[0].isupper() and start - len(word) - 1 > 0)
    return product_tail or camel


def _cadence_safe(after: str) -> bool:
    try:
        return _cadence(after) is not None
    except ComplexParseError:
        return True


_BOUNDARY = re.compile(r"(?<!\d),|,(?!\d)|[;.?!](?!\d)")


def _clause_bounds(text: str, pos: int) -> tuple[int, int]:
    """Clause = text between punctuation; commas inside numbers (1,400) don't count."""
    left = 0
    for m in _BOUNDARY.finditer(text, 0, pos):
        left = m.end()
    m = _BOUNDARY.search(text, pos)
    return left, (m.start() if m else len(text))


def _window(text: str, tok, before_n=5, after_n=4) -> str:
    lo, hi = _clause_bounds(text, tok["start"])
    pre = _words(text[lo:tok["start"]])[-before_n:]
    post = _words(text[tok["end"]:hi])[:after_n]
    return " ".join(pre) + " | " + " ".join(post)


def _cadence(after: str) -> str | None:
    a = re.sub(r"^\s*(dollars?|bucks?|usd)\b", "", after)
    # "5000 net a month", "$90k gross a year": the qualifier sits between the amount and its period
    a = re.sub(r"^\s*(?:(?:net|gross|take[- ]home|after[- ]tax(?:es)?|before[- ]tax(?:es)?|pre[- ]?tax|post[- ]?tax)\b\s*)+", " ", a)[:30]
    if re.search(r"^\s*(/|per |a |each |every )?\s*(hour|hr)\b|/hr|an hour", a):
        raise ComplexParseError("hourly")
    if re.search(r"biweekly|bi-weekly|every two weeks|every other week|every 2 weeks", a):
        return "biweekly"
    if re.search(r"^\s*(/|per |a |each |every )\s*(mo\b|month)|^\s*(/mo|monthly)|\bmonthly\b", a):
        return "monthly"
    if re.search(r"^\s*(/|per |a |each |every )\s*(wk|week)|^\s*weekly|\bweekly\b", a):
        return "weekly"
    if re.search(r"^\s*(/|per |a |each |every )\s*(yr|year)|^\s*(annually|yearly)|\bannual", a):
        return "annual"
    return None


def _cue_distance(pre: list[str], post: list[str], cues, before_n: int = 4, after_n: int = 3) -> int | None:
    """Words between the amount and the nearest cue word (None if no cue is in range)."""
    dists = [i + 1 for i, w in enumerate(reversed(pre[-before_n:])) if _has([w], cues)]
    dists += [i + 1 for i, w in enumerate(post[:after_n]) if _has([w], cues)]
    return min(dists) if dists else None


def _has(words, cues) -> bool:
    if isinstance(words, str):
        words = _words(words)
    return any(w.startswith(c) for w in words for c in cues)


def _month_start(text: str, today: date) -> int | None:
    low = text.lower()
    for name, num in MONTHS.items():
        m = re.search(r"\b" + name + r"\b(?!\s+(i|we|you|someone)\b)(?:\s+(\d{1,2})(?:st|nd|rd|th)?)?", low)
        if name == "may" and m and not re.search(r"\b(in|on|by|for|until|since)\s+may\b|\bmay\s+\d", low):
            continue
        if m:
            day = int(m.group(2)) if m.group(2) else 1
            year = today.year + (1 if (num, day) < (today.month, today.day) else 0)
            try:
                return (date(year, num, day) - today).days
            except ValueError:
                return None
    return None


_HALFWAY = re.compile(r"\b(?:half ?way|midway|mid)[ -]?(?:through|into|way through|of)?\s*(?:the|my|this)?\s*(?:first\s+)?year\b"
                      r"|\bhalf (?:a|of a|the) year\b|\bhalf-year\b|\bmid-?year\b", re.I)
_YEAR_END = re.compile(r"\b(?:end of (?:the|this) year|year[- ]end|new year'?s)\b", re.I)


def _add_months(today: date, months: int) -> date:
    y, m = divmod(today.month - 1 + months, 12)
    year, month = today.year + y, m + 1
    for day in (today.day, 28):
        try:
            return date(year, month, min(day, 28) if day == 28 else day)
        except ValueError:
            continue
    return date(year, month, 28)


def _start_day(clause: str, today: date, toks_in_clause) -> int | None:
    for t in toks_in_clause:
        if t["kind"] == "timing":
            n = int(t["value"])
            if t["unit"] == "month":  # calendar months, so "in 6 months" from Oct 10 is Apr 10
                return (_add_months(today, n) - today).days
            return n * {"day": 1, "week": 7, "year": 365}[t["unit"]]
    low = clause.lower()
    if _HALFWAY.search(low):
        return (_add_months(today, 6) - today).days
    if _YEAR_END.search(low):
        return (date(today.year, 12, 31) - today).days
    ms = _month_start(clause, today)
    if ms is not None:
        return ms
    if "tomorrow" in low:
        return 1
    if "next week" in low:
        return 7
    if "next month" in low:
        nm = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        return (nm - today).days
    if "next year" in low:
        return (date(today.year + 1, 1, 1) - today).days
    if re.search(r"\b(today|now|right away|immediately)\b", low):
        return 0
    return None


def regex_spec(text: str, today: date) -> dict:
    """Build the raw-number spec from the text, or raise ComplexParseError."""
    toks = tokenize(text)
    if not toks:
        raise ComplexParseError("no numbers")
    low = text.lower()
    spec: dict = {"income": [], "expenses": [], "loans": []}

    pcts = [t for t in toks if t["kind"] == "pct"]
    terms = [t for t in toks if t["kind"] == "term"]
    money = [t for t in toks if t["kind"] == "money"]
    for t in terms:
        if t["unit"] in ("week", "day"):
            raise ComplexParseError("unsupported term unit")
    down_pct = [t for t in pcts if re.match(r"\s*(down|as a down|for the down|downpayment)", t["after"])
                or re.search(r"(down ?payment( of| is| was)?|put down)\s*$", t["before"])]
    rates = [t for t in pcts if t not in down_pct]
    if any(not 0 <= t["value"] <= 60 for t in rates):
        raise ComplexParseError("rate out of range")

    # "2 games at $70 each": the count multiplies the price instead of being its own amount.
    qty = {}
    for m in re.finditer(r"\b(\d{1,3})\s+([A-Za-z][\w'-]*(?:\s+[A-Za-z][\w'-]*)?)\s+(?:at|for|@)\s+\$?\s?(\d[\d,]*(?:\.\d+)?)"
                         r"\s*(?:each|apiece|a piece|per)\b", text, re.IGNORECASE):
        count = next((t for t in money if t["start"] <= m.start(1) < t["end"]), None)
        price = next((t for t in money if t["start"] <= m.start(3) < t["end"] + 1 and t is not count), None)
        if count and price:
            qty[id(price)] = (count, m.group(2))
    counts = {id(c) for c, _ in qty.values()}
    money = [t for t in money if id(t) not in counts]

    down_money, payments, purchase, expenses, incomes, financed = [], [], [], [], [], []
    for t in money:
        w = _window(text, t)
        pre, post = (x.split() for x in w.split(" | "))
        lo, hi = _clause_bounds(text, t["start"])
        clause = text[lo:hi]
        cad = _cadence(t["after"])
        if pre and pre[-1].startswith(("financ", "borrow")) and post[:1] == ["of"]:
            financed.append(t)  # "financed 2000 of it": the borrowed share of a purchase
            continue
        if "down" in post[:2] or re.search(r"down( ?payment)?( of| is| was)?$", " ".join(pre[-3:])):
            down_money.append(t)
            continue
        if "payment" in post[:3] and not _has(post[:3], ("down",)):
            payments.append((t, w, clause, cad or "monthly"))
            continue
        # The nearest cue wins: "a $400/month raise and want a $28k car" puts "raise" beside $400, "car" beside $28k.
        d_inc = _cue_distance(pre, post, INCOME_CUES, after_n=4)
        d_buy = _cue_distance(pre, post, PURCHASE_CUES)
        if d_inc is not None and (d_buy is None or d_inc < d_buy):
            incomes.append((t, w, clause, cad))
        elif d_buy is not None and not _has(post[:2], ("rent", "insurance", "lease")):
            purchase.append((t, w, clause, cad))
        else:
            expenses.append((t, w, clause, cad))

    # Global fallback start for purchases/expenses with no timing in their own clause.
    global_start = _start_day(text, today, [t for t in toks if t["kind"] == "timing"])

    def start_for(clause, default):
        s = _start_day(clause, today, [t for t in toks if t["kind"] == "timing"
                                       and clause.lower().find(t["text"].lower().strip()) != -1])
        return default if s is None else max(0, min(MAX_START_DAY, s))

    for t, w, clause, cad in incomes:
        cl = clause.lower()
        if cad is None:
            if _has(cl, ONE_TIME_INCOME_CUES):
                cad = "once"
            elif t["value"] >= 20000:
                cad = "annual"
            else:
                raise ComplexParseError("income without period")
        replaces = cad != "once" and not _has(cl, ADDITIVE_CUES) and any(c in " " + cl + " " for c in REPLACE_CUES)
        spec["income"].append({"amount": t["value"], "period": cad, "replaces": replaces,
                               "net": any(n in low for n in NET_WORDS), "start_day": 0 if purchase else start_for(clause, 0),
                               "label": _income_label(cl, cad)})

    loans = [{"tok": t, "clause": clause, "cad": cad, "down": [], "down_pct": [], "rate": [], "term": [], "payment": [],
              "financed": []}
             for t, w, clause, cad in sorted(purchase, key=lambda x: x[0]["start"])]

    def owner(tok):
        """Attributes belong to the nearest purchase mentioned before them (else the first one)."""
        before = [i for i, ln in enumerate(loans) if ln["tok"]["start"] <= tok["start"]]
        return loans[before[-1] if before else 0]

    if financed and not loans:
        # "buy a used car for 4000, but financed 2000 of it": the purchase is the first plain amount
        if not expenses:
            raise ComplexParseError("financed amount without a purchase")
        t, w, clause, cad = expenses.pop(0)
        loans.append({"tok": t, "clause": clause, "cad": cad, "down": [], "down_pct": [], "rate": [], "term": [],
                      "payment": [], "financed": []})
    if loans:
        for t in financed:
            owner(t)["financed"].append(t)
        for t in down_money:
            owner(t)["down"].append(t)
        for t in down_pct:
            owner(t)["down_pct"].append(t)
        for t in rates:
            owner(t)["rate"].append(t)
        for t in terms:
            owner(t)["term"].append(t)
        for t, w, clause, cad in payments:
            owner(t)["payment"].append(t)
    else:
        if down_money or down_pct or rates:
            raise ComplexParseError("rate/down payment without a loan amount")
        expenses += [(t, w, clause, cad) for t, w, clause, cad in payments]
        if len(terms) > 1:
            raise ComplexParseError("several terms without loans")

    for t, w, clause, cad in expenses:
        months = None
        if terms and cad and terms[0]["unit"] in ("month", "year"):
            months = round(terms[0]["value"] * (12 if terms[0]["unit"] == "year" else 1))
        if cad is None and re.search(r"\b(rent|subscription|premium|insurance)\b", w):
            cad = "monthly"
        count, noun = qty.get(id(t), (None, None))
        spec["expenses"].append({"amount": t["value"], "cadence": cad or "once", "months": months,
                                 "quantity": count["value"] if count else None,
                                 "start_day": start_for(clause, global_start or 0),
                                 "label": clean_label(noun, "Expense") if noun else _item_label(text, t, clause, "Expense")})
    if not loans and terms and not any(e["months"] for e in spec["expenses"]):
        raise ComplexParseError("stray term")

    for ln in loans:
        t = ln["tok"]
        if any(len(ln[k]) > 1 for k in ("down", "down_pct", "rate", "term", "payment", "financed")) or (
                sum(bool(ln[k]) for k in ("down", "down_pct", "financed")) > 1):
            raise ComplexParseError("a loan has two rates, terms, payments or down payments")
        term = ln["term"][0] if ln["term"] else None
        years = None if term is None else (term["value"] / 12 if term["unit"] == "month" else term["value"])
        loan = {"price": t["value"], "down": ln["down"][0]["value"] if ln["down"] else None,
                "down_pct": ln["down_pct"][0]["value"] if ln["down_pct"] else None,
                "rate_pct": ln["rate"][0]["value"] if ln["rate"] else None, "years": years,
                "payment": ln["payment"][0]["value"] if ln["payment"] else None,
                "financed": ln["financed"][0]["value"] if ln["financed"] else None,
                "start_day": start_for(ln["clause"], global_start or 0),
                "label": _item_label(text, t, ln["clause"], "Loan")}
        plain = all(loan[k] is None for k in ("down", "down_pct", "rate_pct", "years", "payment", "financed"))
        if plain:  # a cash purchase of a house/car/etc.
            spec["expenses"].append({"amount": t["value"], "cadence": ln["cad"] or "once", "months": None,
                                     "start_day": loan["start_day"], "label": loan["label"]})
        elif loan["rate_pct"] is not None and loan["payment"] is not None:
            raise ComplexParseError("a loan has both a rate and a monthly payment")
        elif years is None and not is_home_loan({"label": f'{loan["label"]} {ln["clause"]}', "years": 0}):
            # Only a home loan gets a default (30 years); ask for anything else's term instead of guessing.
            raise LoanTermMissing(loan["label"], loan["price"], loan["financed"])
        else:
            # A missing rate (or a home loan's missing term) is filled in from the person's own settings
            # by build_decision, and the answer says so.
            spec["loans"].append(loan)

    check_coverage(text, spec)
    return spec


_FILLER = {"a", "an", "the", "my", "new", "dollar", "dollars", "bucks", "usd", "for", "on", "of", "to", "in", "worth",
           "per", "each", "every", "month", "monthly", "week", "weekly", "year", "yearly", "today", "tomorrow", "now",
           "and", "but", "then", "so", "with", "that", "this", "is", "was", "be"}


_LABEL_STOP = {"at", "over", "with", "from", "by", "if", "could", "can", "should", "would", "will", "i", "we", "you",
               "starting", "start", "around", "next", "before", "after", "until", "within", "each", "apiece", "or"}


def _item_label(text: str, tok: dict, clause: str, default: str) -> str:
    """What the amount is for: "$1999 iPhone 18 Pro" -> "iPhone 18 Pro", "rent of $1200" -> "Rent"."""
    lo, hi = _clause_bounds(text, tok["start"])
    words = re.findall(r"[A-Za-z][\w'-]*|\d+", text[tok["end"]:hi])
    picked: list[str] = []
    for w in words:
        if w.lower() in ("and", "or", "plus", "but", "then"):
            break  # the next item starts here: "$499 and 2 games"
        if w.lower() in _FILLER and not picked:
            continue
        if w.lower() in _FILLER or w.lower() in _LABEL_STOP:
            break
        picked.append(w)
        if len(picked) == 4:
            break
    if not picked:
        before = re.findall(r"[A-Za-z][\w'-]*", text[lo:tok["start"]])
        picked = [w for w in before[-2:] if w.lower() not in _FILLER and w.lower() not in AMOUNT_WORDS
                  and w.lower() not in _LABEL_STOP and len(w) > 2][-1:]
    return clean_label(" ".join(picked), default)


def _income_label(clause: str, period: str) -> str:
    for cue, name in (("bonus", "Bonus"), ("raise", "Raise"), ("gift", "Gift"), ("refund", "Refund"),
                      ("inherit", "Inheritance"), ("prize", "Prize"), ("sold", "Sale")):
        if cue in clause:
            return name
    return "One-time income" if period == "once" else "Income"


def clean_label(label, default: str) -> str:
    """Short, display-safe label; falls back to `default` for anything odd."""
    label = re.sub(r"\s+", " ", str(label or "")).strip(" .,!?:;\"'")
    label = re.sub(r"^(a|an|the|my|our)\s+", "", label, flags=re.IGNORECASE)
    if not label or len(label) > 40 or "?" in label or "$" in label or label.lower() in PRONOUNS:
        return default
    if not (len(label) > 1 and label[0].islower() and label[1].isupper()):  # keep "iPhone", "eBay"
        label = label[0].upper() + label[1:]
    return label


def spec_numbers(spec: dict) -> Counter:
    """Numbers the spec accounts for, in the same units tokenize() reads from text."""
    c: Counter = Counter()
    for i in spec.get("income", []):
        c[("money", float(i["amount"]))] += 1
    for e in spec.get("expenses", []):
        c[("money", float(e["amount"]))] += 1
        if e.get("quantity") not in (None, 1, 1.0):
            c[("money", float(e["quantity"]))] += 1
        if e.get("months"):
            c[("term", float(e["months"]))] += 1
    for loan in spec.get("loans", []):
        c[("money", float(loan["price"]))] += 1
        if loan.get("down"):
            c[("money", float(loan["down"]))] += 1
        if loan.get("payment"):
            c[("money", float(loan["payment"]))] += 1
        if loan.get("financed"):
            c[("money", float(loan["financed"]))] += 1
        if loan.get("down_pct") is not None:
            c[("pct", float(loan["down_pct"]))] += 1
        if loan.get("rate_pct") is not None:
            c[("pct", float(loan["rate_pct"]))] += 1
        if loan.get("years") is not None:
            c[("term", round(float(loan["years"]) * 12, 4))] += 1
    return c


def text_numbers(text: str) -> Counter:
    c: Counter = Counter()
    for t in tokenize(text):  # "name" tokens (iPhone 18) are not amounts and need no accounting
        if t["kind"] == "money":
            c[("money", t["value"])] += 1
        elif t["kind"] == "pct":
            c[("pct", t["value"])] += 1
        elif t["kind"] == "term":
            c[("term", round(t["value"] * (12 if t["unit"] == "year" else 1), 4))] += 1
    return c


def check_coverage(text: str, spec: dict) -> None:
    want, got = text_numbers(text), spec_numbers(spec)
    # The LLM may set aside bare numbers that are names ("iPhone 18", "2 kids") under "ignored", but
    # never a $ amount, a "k" amount, a percentage or a loan term.
    names = Counter(t["value"] for t in tokenize(text) if t["kind"] == "name")
    bare = Counter(t["value"] for t in tokenize(text) if t["kind"] == "money" and not t["dollar"]
                   and not re.search(r"[kK]\s*$", t["text"]) and not re.match(r"\s*(dollars?|bucks?|usd)\b", t["after"]))
    timing = Counter(t["value"] for t in tokenize(text) if t["kind"] in ("timing", "date"))
    for value in spec.get("ignored", []) or []:
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ComplexParseError("ignored a non-number")
        if names[value] > 0:
            names[value] -= 1
        elif timing[value] > 0:  # "in 6 months": the 6 sets a start date, it isn't an amount
            timing[value] -= 1
        elif bare[value] > 0:
            bare[value] -= 1
            want[("money", value)] -= 1
            if want[("money", value)] <= 0:
                del want[("money", value)]
        else:
            toks = tokenize(text)
            as_term = any(t["kind"] == "term" and t["value"] == value for t in toks)
            as_money = any(t["kind"] == "money" and t["value"] == value for t in toks)
            as_pct = any(t["kind"] == "pct" and t["value"] == value for t in toks)
            why = ("it is a loan term, so put it in that loan's \"years\" (months / 12), not in \"ignored\"" if as_term
                   else "it is a rate, so put it in \"rate_pct\"" if as_pct
                   else "it is a money amount, so it belongs in an income, expense or loan field" if as_money
                   else "that number isn't in the text")
            raise ComplexParseError(f"you put {value:g} in \"ignored\", but {why}")
    # an expense with `months` shares its term token with nothing else; compare as multisets
    if want != got:
        raise ComplexParseError(f"unaccounted numbers: text={dict(want)} spec={dict(got)}")


# --------------------------------------------------------------------------- #
# spec -> Decision (all math here, deterministic)
# --------------------------------------------------------------------------- #

_PERIOD_DAYS = {"weekly": 7, "biweekly": 14, "monthly": DAYS_PER_MONTH, "annual": 365}


def _clamp_start(v) -> int:
    try:
        return max(0, min(MAX_START_DAY, int(v)))
    except (TypeError, ValueError):
        return 0


_CASH_LOAN_LABELS = {"loan", "personal loan", "borrowed money", "borrowing", "money", "cash", "debt", "line of credit"}


def _is_cash_loan(label: str) -> bool:
    low = label.strip().lower()
    return low in _CASH_LOAN_LABELS or low.startswith("borrow") or low.endswith(" loan") and low.split()[0] in ("personal", "bank", "payday", "cash")


MORTGAGE_DEFAULT_YEARS = 30


def build_decision(spec: dict, text: str, confidence: float = 0.85, default_net_factor: float = NET_FACTOR,
                   mortgage_apr: float = DEFAULT_RATES["mortgage"],
                   personal_loan_apr: float = DEFAULT_RATES["personal"]) -> Decision:
    items: list[DecisionDelta] = []
    terms: dict = {"assumptions": []}
    replace_income = []

    for inc in spec.get("income", []):
        amount, period = float(inc["amount"]), inc["period"]
        if amount <= 0 or period not in ("once", "annual", "monthly", "weekly", "biweekly"):
            raise ValueError("bad income")
        net_factor = 1.0 if inc.get("net") else default_net_factor
        if period == "once":  # a bonus or gift: one deposit, after tax unless the text says take-home
            items.append(DecisionDelta(kind="one_time", amount=round(amount * net_factor, 2),
                                       start_day=_clamp_start(inc.get("start_day")),
                                       label=clean_label(inc.get("label"), "One-time income"), raw_text=text,
                                       confidence=confidence, direction="inflow"))
            if net_factor != 1.0 and "income_tax_assumed" not in terms["assumptions"]:
                terms["assumptions"].append("income_tax_assumed")
                terms["net_factor"] = net_factor
            continue
        # An annual salary is paid monthly; everything else keeps its own cadence.
        if period in ("annual", "monthly"):  # a "month" of pay is 1/12 of a year, so $500/month shows as $500
            per_pay = (amount / 12 if period == "annual" else amount) * net_factor
            cadence = DAYS_PER_YEAR_MONTHLY
        else:
            per_pay, cadence = amount * net_factor, float(_PERIOD_DAYS[period])
        item = DecisionDelta(kind="recurring", amount=round(per_pay, 2), start_day=_clamp_start(inc.get("start_day")),
                             cadence_days=cadence, label="New income" if inc.get("replaces") else "Extra income",
                             raw_text=text, confidence=confidence, direction="inflow")
        if inc.get("replaces"):
            replace_income.append({"index": len(items), "annual": round(per_pay * 365 / cadence, 2)})
        items.append(item)
        if net_factor != 1.0 and "income_tax_assumed" not in terms["assumptions"]:
            terms["assumptions"].append("income_tax_assumed")
            terms["net_factor"] = net_factor

    for e in spec.get("expenses", []):
        quantity = float(e.get("quantity") or 1)
        if not 1 <= quantity <= 100:
            raise ValueError("bad quantity")
        amount, cad = round(float(e["amount"]) * quantity, 2), e.get("cadence") or "once"
        if amount <= 0:
            raise ValueError("bad expense")
        start = _clamp_start(e.get("start_day"))
        label = clean_label(e.get("label"), "Expense")
        if cad == "once":
            items.append(DecisionDelta(kind="one_time", amount=amount, start_day=start, label=label,
                                       raw_text=text, confidence=confidence))
        else:
            months = e.get("months")
            end = start + int(months) * DAYS_PER_MONTH if months else None
            items.append(DecisionDelta(kind="recurring", amount=amount, start_day=start,
                                       cadence_days=_PERIOD_DAYS[cad], end_day=end, label=label,
                                       raw_text=text, confidence=confidence))

    loan_terms = []
    for loan in spec.get("loans", []):
        price = float(loan["price"])
        down = (float(loan["down"]) if loan.get("down") else
                price * float(loan["down_pct"]) / 100 if loan.get("down_pct") is not None else
                price - float(loan["financed"]) if loan.get("financed") else 0.0)
        if price <= 0 or down < 0 or down >= price:
            raise ValueError("bad loan")
        stated = float(loan["payment"]) if loan.get("payment") else None
        loan_label = clean_label(loan.get("label"), "Loan")
        home_loan = is_home_loan({"label": loan_label, "years": loan.get("years") or 0})
        years = loan.get("years")
        assumed = {}
        if years is None:
            if not home_loan:
                raise LoanTermMissing(loan_label, price, float(loan["financed"]) if loan.get("financed") else None)
            years = assumed["years"] = MORTGAGE_DEFAULT_YEARS
        if stated:
            rate = None
        elif loan.get("rate_pct") is not None:
            rate = float(loan["rate_pct"])
        else:  # not stated: this person's typical rate for this kind of loan
            rate = assumed["rate_pct"] = round((mortgage_apr if home_loan else personal_loan_apr) * 100, 4)
            assumed["kind"] = "mortgage" if home_loan else "personal loan"
        loan = {**loan, "years": years}
        # A home loan's money goes to the seller; any other loan puts cash in the user's account.
        # A loan taken to buy something (house, car, laptop) pays the seller; only plain borrowing lands as cash.
        is_home = is_home_loan({"label": loan_label, "years": loan["years"]}) or not _is_cash_loan(loan_label)
        sub = loan_decision(price - down, None if rate is None else rate / 100, float(loan["years"]),
                            payment=stated, down_payment=round(down, 2), start_day=_clamp_start(loan.get("start_day")),
                            label=loan_label, raw_text=text, confidence=confidence,
                            proceeds_to_user=not is_home)
        offset = len(items)
        items.extend(sub.items)
        if assumed:
            terms.setdefault("assumed_loans", []).append({"label": loan_label, **assumed})
        loan_terms.append({**sub.terms, "price": price, "rate_pct": rate,
                           "item_indexes": list(range(offset, len(items))), "label": loan_label, "start_day": _clamp_start(loan.get("start_day"))})
    if loan_terms:
        terms["loans"] = loan_terms
    if replace_income:
        terms["replace_income"] = replace_income
    if not items:
        raise ValueError("nothing to decide")
    if len(items) > 8:
        raise ValueError("too many items")
    return Decision(items=tuple(items), label=text[:50], raw_text=text, confidence=confidence, terms=terms)


# --------------------------------------------------------------------------- #
# Gemini reads the question; code checks every number and does all the math.
# --------------------------------------------------------------------------- #

_LLM_PROMPT = """You turn a personal-finance question into JSON fields. You never do math or date arithmetic:
copy every number exactly as written (90k -> 90000, "5 years" -> 5, "7 percent" -> 7) into exactly one field.
Questions are casual, may have typos, slang, hypotheticals and several items in one run-on sentence. Read the
WHOLE message, give every item its own timing, and account for every number.

Fields:
- "income": money coming IN. {{"amount", "period": "once" (bonus, gift, refund, one paycheck) | "weekly" |
  "biweekly" | "monthly" | "annual", "replaces": true only for a new job/salary that replaces current pay,
  "net": true if the text says net / take-home / after tax, "start", "label"}}
- "expenses": money going OUT. {{"amount" (price of ONE), "quantity" (how many, when the text says "2 games at
  $70 each"; else null), "cadence": "once" | "weekly" | "biweekly" | "monthly" | "annual", "months": number of
  payments if the text limits it else null, "start", "label"}}
- "loans": something bought with a loan or mortgage, or money borrowed. {{"price", "down", "down_pct",
  "rate_pct", "payment" (monthly payment ONLY if the text states it instead of a rate), "years" (the term),
  "financed" (the amount borrowed when the text says "financed/borrowed X of it"; else null),
  "start", "label"}}. A $3000 loan repaid with $300 payments over 1 year is price 3000, payment 300, years 1.
  Leave "rate_pct" and "years" null when the message doesn't state them; never guess a rate or a term
  ("a $400k house with 20% down" -> rate_pct null, years null; the app fills in the person's own rate).
  The borrowed part of a purchase is part of that loan, never a second expense: "buy a used car for 4000 but
  finance 2000 of it" -> loans: price 4000, financed 2000, years null.
- "ignored": numbers that are NOT money, a quantity, a rate or a loan term: model numbers ("iPhone 18" -> 18),
  "2 kids", and the number inside a start time ("in 6 months" -> 6). Never a $ amount, a "k" amount, a
  percentage or a loan length.

"start" says WHEN that item begins, as the user phrased it. Code turns it into a date, so never compute one.
- {{"in": 6, "unit": "months"}} for relative timing. unit is days | weeks | months | years. Examples: "in 6
  months" -> 6 months; "halfway into the first year", "mid-year", "in half a year" -> 6 months; "a year from
  now" -> 1 year; "next week" -> 1 week; "in a couple of months" -> 2 months; "right away", "now", "today" or
  no timing at all -> {{"in": 0, "unit": "days"}}.
- {{"date": "YYYY-MM-DD"}} only when the text names a calendar date ("on March 3rd", "in December" -> the next
  December 1st, "end of the year" -> December 31). Today is {today} ({weekday}).
- A timing phrase applies to the item it is attached to ("I put the down payment down halfway into the first year"
  times the house loan, whose down payment and monthly payments both begin then). A timing phrase for the
  whole question applies to every item that has none of its own. A new job or raise starts now unless the text
  says otherwise.

"label" is 1-4 words naming the thing, like "iPhone 18 Pro", "Rent", "Car", "Bonus". Never a whole sentence or a
verb; if the message doesn't name the thing, use "Purchase" (or "Income" for money coming in).
Hypotheticals count: "if I made $3000 off a bonus" is income of 3000, period "once". If it's not a money
question, return all lists empty.

Examples (today 2026-10-10, a Saturday):
"If I made $3000 off of a bonus, could I buy a $1999 iPhone 18 Pro" ->
{{"income": [{{"amount": 3000, "period": "once", "replaces": false, "net": false, "start": {{"in": 0, "unit": "days"}}, "label": "Bonus"}}],
 "expenses": [{{"amount": 1999, "quantity": null, "cadence": "once", "months": null, "start": {{"in": 0, "unit": "days"}}, "label": "iPhone 18 Pro"}}],
 "loans": [], "ignored": [18]}}
"thinking about a PS5 for $499 and 2 games at $70 each" ->
{{"income": [], "expenses": [{{"amount": 499, "quantity": null, "cadence": "once", "months": null, "start": {{"in": 0, "unit": "days"}}, "label": "PS5"}},
 {{"amount": 70, "quantity": 2, "cadence": "once", "months": null, "start": {{"in": 0, "unit": "days"}}, "label": "Games"}}], "loans": [], "ignored": [5]}}
"can I swing $65/mo for spotify and netflix starting next month" ->
{{"income": [], "expenses": [{{"amount": 65, "quantity": null, "cadence": "monthly", "months": null, "start": {{"in": 1, "unit": "months"}}, "label": "Spotify and Netflix"}}], "loans": [], "ignored": []}}
"new job pays 85k, want a $30k car with 10% down at 6% for 5 years" ->
{{"income": [{{"amount": 85000, "period": "annual", "replaces": true, "net": false, "start": {{"in": 0, "unit": "days"}}, "label": "New job"}}],
 "expenses": [], "loans": [{{"price": 30000, "down": null, "down_pct": 10, "rate_pct": 6, "payment": null, "years": 5, "start": {{"in": 0, "unit": "days"}}, "label": "Car"}}], "ignored": []}}
"I make 3000 net a month at my new job and want a 300000 house, 30 year mortgage at 7 percent, 6000 down in 6 months" ->
{{"income": [{{"amount": 3000, "period": "monthly", "replaces": true, "net": true, "start": {{"in": 0, "unit": "days"}}, "label": "New job"}}],
 "expenses": [], "loans": [{{"price": 300000, "down": 6000, "down_pct": null, "rate_pct": 7, "payment": null, "years": 30, "start": {{"in": 6, "unit": "months"}}, "label": "House"}}], "ignored": [6]}}
"i can take a 3000 dollar loan now with 300 a month payments over a year, then in 6 months put 6000 down on a 300000 house, 30 years at 7 percent" ->
{{"income": [], "expenses": [], "loans": [{{"price": 3000, "down": null, "down_pct": null, "rate_pct": null, "payment": 300, "years": 1, "start": {{"in": 0, "unit": "days"}}, "label": "Loan"}},
 {{"price": 300000, "down": 6000, "down_pct": null, "rate_pct": 7, "payment": null, "years": 30, "start": {{"in": 6, "unit": "months"}}, "label": "House"}}], "ignored": [6]}}
(When only the down payment or purchase has a timing phrase, a separate small loan the user "takes" keeps its own
start, which is now unless said otherwise.)

Message: {text}"""


_UNIT_DAYS = {"day": 1, "week": 7, "year": 365}


def _start_from(entry: dict, today: date) -> int:
    """Day offset from the model's "start": relative {"in", "unit"} or {"date"}; legacy "start_date"/"start_day" still work."""
    start = entry.get("start")
    if isinstance(start, dict):
        if start.get("date"):
            entry = {**entry, "start_date": start["date"]}
        else:
            try:
                n, unit = float(start.get("in") or 0), str(start.get("unit") or "days").lower().rstrip("s")
            except (TypeError, ValueError):
                n, unit = 0.0, "day"
            if unit == "month":
                return max((_add_months(today, round(n)) - today).days, 0)
            return max(round(n * _UNIT_DAYS.get(unit, 1)), 0)
    raw = entry.get("start_date")
    if isinstance(raw, str) and raw.strip():
        try:
            return (date.fromisoformat(raw.strip()[:10]) - today).days
        except ValueError:
            pass
    try:
        return int(entry.get("start_day") or 0)
    except (TypeError, ValueError):
        return 0


def timing_hint(text: str, today: date) -> int | None:
    """Days until the start the text itself implies ("in 6 months", "halfway into the year"), if it has one."""
    toks = [t for t in tokenize(text) if t["kind"] == "timing"]
    days = _start_day(text, today, toks)
    return days if days and days > 0 else None


def _spec_from_model(raw, text: str, today: date, last_try: bool) -> dict:
    if not isinstance(raw, dict):
        raise ComplexParseError("the answer was not a JSON object")
    spec = {k: [x for x in raw.get(k, []) or [] if isinstance(x, dict)] for k in ("income", "expenses", "loans")}
    spec["ignored"] = [x for x in raw.get("ignored", []) or [] if isinstance(x, (int, float, str))]
    for entry in (*spec["income"], *spec["expenses"], *spec["loans"]):
        entry["start_day"] = _start_from(entry, today)
    check_coverage(text, spec)  # hallucinated or dropped numbers are rejected here
    hint = timing_hint(text, today)
    if hint and not any(e["start_day"] > 0 for k in ("income", "expenses", "loans") for e in spec[k]):
        if not last_try:
            raise ComplexParseError(f"the text puts something about {hint} days from now, but every start is today; "
                                    "give the item that timing a \"start\"")
        # Still nothing after one correction: trust the words over the model for purchases and loans.
        for entry in (*spec["expenses"], *spec["loans"]):
            entry["start_day"] = hint
    return spec


def llm_spec(text: str, today: date) -> dict:
    """Ask Gemini; if the answer fails a check, tell it exactly what was wrong and ask once more."""
    if not os.getenv("GEMINI_API_KEY"):
        raise ComplexParseError("no LLM available")
    from llm.gemini import generate_text
    prompt = _LLM_PROMPT.format(today=today.isoformat(), weekday=today.strftime("%A"), text=text)
    problem = None
    for attempt in range(2):
        ask = prompt if problem is None else (
            f"{prompt}\n\nYour previous answer was rejected: {problem}. Fix exactly that and answer again with JSON only.")
        try:
            raw = json.loads(generate_text(ask, max_output_tokens=900, json_mode=True))
            return _spec_from_model(raw, text, today, last_try=attempt == 1)
        except ComplexParseError as exc:
            problem = str(exc)
            if attempt == 1:
                raise
        except json.JSONDecodeError:
            problem = "the answer was not valid JSON"
            if attempt == 1:
                raise ComplexParseError(problem) from None
    raise ComplexParseError(problem or "no answer")


class NotAFinanceQuestion(ComplexParseError):
    """Gemini found nothing to simulate (no amounts, or not about money)."""


def parse_with_gemini(text: str, today: date, net_factor: float = NET_FACTOR, **rates: float) -> Decision:
    """Gemini fills the fields; code checks the numbers and builds the decision. Raises ComplexParseError."""
    raw_text, text = text, normalize_text(text)
    if not any(t["kind"] == "money" for t in tokenize(text)):
        raise NotAFinanceQuestion("no amount")  # nothing for Gemini to read; skip the call
    spec = llm_spec(text, today)
    if not any(spec[k] for k in ("income", "expenses", "loans")):
        raise NotAFinanceQuestion("nothing to simulate")
    for entry in (*spec["income"], *spec["expenses"]):
        if not isinstance(entry.get("amount"), (int, float)) or entry["amount"] <= 0:
            raise ComplexParseError("an item has no amount")
    for loan in spec["loans"]:
        if not isinstance(loan.get("price"), (int, float)) or loan["price"] <= 0:
            raise ComplexParseError("a loan has no amount")
    return build_decision(spec, raw_text, 0.8, net_factor, **rates)


def parse_complex(text: str, today: date, *, use_llm: bool = True, net_factor: float = NET_FACTOR, **rates: float) -> Decision:
    """Regex grammar, then LLM fallback. Raises ComplexParseError if neither can account for every number."""
    errors: list[Exception] = []
    raw_text, text = text, normalize_text(text)
    try:
        return build_decision(regex_spec(text, today), raw_text, 0.85, net_factor, **rates)
    except (ComplexParseError, ValueError, KeyError) as exc:
        errors.append(exc)
    if use_llm:
        try:
            return build_decision(llm_spec(text, today), raw_text, 0.7, net_factor, **rates)
        except Exception as exc:  # network, JSON, coverage, validation
            errors.append(exc)
    # A specific question ("how long would you take to pay it off?") beats the generic hint.
    missing = next((e for e in errors if isinstance(e, LoanTermMissing)), None)
    raise ComplexParseError(str(missing) if missing else HINT)


def needs_complex(text: str) -> bool:
    """Route to the complex parser unless this is the plain single-expense shape."""
    text = normalize_text(text)
    toks = tokenize(text)
    low = text.lower()
    flagged = (_has(_words(low), INCOME_CUES + ("interest", "apr", "down", "mortgage", "loan", "financ", "borrow"))
               or any(t["kind"] in ("pct", "term", "timing") for t in toks))
    money = [t for t in toks if t["kind"] == "money"]
    others = [t for t in toks if t["kind"] not in ("money", "date")]
    return flagged or len(money) != 1 or bool(others)
