"""Integration seams for Dev C; no Gemini prompts or SDK calls live here."""
from __future__ import annotations

import hashlib
import importlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

from core.feasible import Outlook
from core.financing import FinancingResult
from core.model import Claim, Decision, DecisionDelta, SimResult, as_decision
from core.run import run
from data.demo import TEST_TRIGGER_CUSTOMERS

logger = logging.getLogger(__name__)


NO_AMOUNT = ("Please include a dollar amount: tell me what you're thinking of buying and what it costs, "
             "like \"Can I afford a $400 phone today?\"")


class RephraseRequired(ValueError):
    pass


RENDER_CACHE_PATH = Path(__file__).resolve().parent.parent / "llm" / "render_cache.json"


def claims_cache_key(claims: list[Claim]) -> str:
    """Stable hash of (kind, value, unit) across claims, order-independent.

    Sorted by kind (always unique within one SimResult.claims, per the
    frozen vocabulary) so the sort itself never needs to compare value,
    which is `float | str` and not orderable across claims.
    """
    ordered = sorted(claims, key=lambda c: c.kind)
    normalized = [[c.kind, c.value, c.unit] for c in ordered]
    # Old cached paragraphs predate the readable response format.
    blob = json.dumps({"copy_version": 4, "claims": normalized}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _load_render_cache() -> dict:
    try:
        return json.loads(RENDER_CACHE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def get_cached_render(cache_key: str) -> str | None:
    return _load_render_cache().get(cache_key)


def save_cached_render(cache_key: str, prose: str) -> None:
    cache = _load_render_cache()
    cache[cache_key] = prose
    RENDER_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RENDER_CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))


def explicit_parse(text: str) -> DecisionDelta:
    """Offline unblocking fake: only accepts complete, explicit forms, never guesses dates."""
    pattern = (
        r"(?:can i afford|spend)\s+\$(?P<amount>\d+(?:,\d{3})*(?:\.\d{1,2})?)"
        r"(?P<cadence>/(?:month|mo|week|year))?\s+"
        r"(?:starting\s+)?(?P<date>today|in (?P<days>\d+) days)\??"
    )
    match = re.fullmatch(pattern, text.strip(), re.IGNORECASE)
    if not match:
        raise RephraseRequired("Please include an amount and timing, for example: Can I afford $1400 today? "
                               "Offline mode also supports $65/month starting today or in 30 days.")
    cadence = {"/month": 30, "/mo": 30, "/week": 7, "/year": 365}.get((match["cadence"] or "").lower())
    return DecisionDelta(kind="recurring" if cadence else "one_time",
                         amount=float(match["amount"].replace(",", "")),
                         start_day=int(match["days"] or 0), cadence_days=cadence,
                         label="Explicit expense", raw_text=text, confidence=1.0)


def parse(text: str, mode: str = "auto", net_factor: float | None = None, *, settings=None) -> DecisionDelta | Decision:
    """Gemini reads the question first ("auto"/"llm"); the rule-based parsers are the offline fallback ("rules").

    Gemini only fills in fields. Code checks that every number in the text is accounted for and does all
    the math (llm/parse_complex.py), so a misread question is refused, never silently simulated.
    """
    if mode == "explicit":
        return explicit_parse(text)
    try:
        complex_parser = importlib.import_module("llm.parse_complex")
    except ModuleNotFoundError:
        complex_parser = None
    if complex_parser is not None and mode in ("auto", "llm") and os.getenv("GEMINI_API_KEY"):
        try:
            return complex_parser.parse_with_gemini(text, date.today(), **_personal(settings, net_factor))
        except complex_parser.NotAFinanceQuestion:
            raise RephraseRequired(NO_AMOUNT) from None
        except Exception as exc:  # network, quota, bad JSON, or numbers it couldn't account for
            logger.info("gemini_parse_fallback error_type=%s", type(exc).__name__)
            if mode == "llm":
                raise RephraseRequired(complex_parser.HINT) from None
    # Offline rules. Anything beyond one plain expense (income, loans, several amounts, rates, terms) goes to
    # the complex grammar, which refuses rather than silently dropping a number.
    if complex_parser is not None and complex_parser.needs_complex(text):
        try:
            return complex_parser.parse_complex(text, date.today(), **_personal(settings, net_factor))
        except complex_parser.ComplexParseError as exc:
            raise RephraseRequired(str(exc)) from None
    try:
        module = importlib.import_module("llm.parse")
    except ModuleNotFoundError as exc:
        if mode == "auto" and exc.name in ("llm", "llm.parse"):
            return explicit_parse(text)
        raise RephraseRequired("The decision parser is unavailable. Please try again when it is configured.") from None
    return module._parse_heuristic(text, date.today())


def _personal(settings, net_factor: float | None) -> dict:
    """This person's tax share and loan rates (api.settings.Settings), when known; else the parser's defaults."""
    kwargs = {} if settings is None else {"net_factor": settings.income_net_factor,
                                          "mortgage_apr": settings.mortgage_apr,
                                          "personal_loan_apr": settings.personal_loan_apr}
    if net_factor is not None:
        kwargs["net_factor"] = net_factor
    return kwargs


def render(claims, culprit):
    return importlib.import_module("llm.render").render(claims, culprit)


def fake_gemini_render(claims, culprit):
    """Deterministic, network-free renderer: the test1-5 trigger's prose
    source, and a fast/realistic stand-in for `render` in tests."""
    return importlib.import_module("llm.render").fake_gemini_render(claims, culprit)


def validate(prose, claims, culprit=None):
    """Validate prose against claims. Culprit is required for full validation."""
    return importlib.import_module("llm.validate").validate(prose, claims, culprit, txn_resolver=None)


def test_fixture_customer(text: str) -> str | None:
    """customer_id to use for a test-trigger request, or None if text isn't one.

    Strips everything but letters/digits before matching: phone autocorrect
    reliably mangles a bare "test1" into "Test 1" (inserted space) or "test1."
    (trailing period) on send, and an exact-match lookup would silently miss
    both, falling through to the real parser and surfacing a confusing
    "parsing was unavailable" error on what the user typed as a trigger.
    """
    normalized = re.sub(r"[^a-z0-9]", "", text.strip().lower())
    return TEST_TRIGGER_CUSTOMERS.get(normalized)


def test_fixture_delta(text: str) -> DecisionDelta:
    """Fixed decision for test-trigger requests.

    Deliberately the same decision "Can I afford $1400 today?" already
    produces via explicit_parse - test_each_fixture_can_be_asked_without_network
    already proves this succeeds end-to-end against all 5 demo_* profiles,
    so this is the lowest-risk choice rather than a new untested scenario.
    """
    return DecisionDelta(kind="one_time", amount=1400.0, start_day=0,
                         label="Test fixture expense", raw_text=text, confidence=1.0)


def validate_decision(decision: Decision | DecisionDelta | dict) -> Decision:
    """Validate every item of a (possibly multi-item) decision; a lone delta becomes a one-item decision."""
    if isinstance(decision, dict):
        decision = Decision.from_dict(decision)
    if isinstance(decision, DecisionDelta):
        return Decision.single(validate_delta(decision))
    if not isinstance(decision, Decision) or not 1 <= len(decision.items) <= 8:
        raise RephraseRequired("Please rephrase with a specific amount and date.")
    if not math.isfinite(decision.confidence) or not 0.5 <= decision.confidence <= 1:
        raise RephraseRequired("Please rephrase with a positive amount and a clear date or recurrence.")
    for item in decision.items:
        if item.end_day is not None and not math.isfinite(item.end_day):
            raise RephraseRequired("Please rephrase with a positive amount and a clear date or recurrence.")
        validate_delta(item)
    for loan in decision.terms.get("loans", []):
        if not ((loan["annual_rate"] is None or 0 <= loan["annual_rate"] <= 0.6)
                and 0 < loan["years"] <= 50 and 0 <= loan["down_payment"] < loan["price"]):
            raise RephraseRequired("Those loan terms don't look right. Please check the rate, term and down payment.")
    return decision


def validate_delta(delta: DecisionDelta | dict) -> DecisionDelta:
    if isinstance(delta, dict):
        delta = DecisionDelta.from_dict(delta)
    if not isinstance(delta, DecisionDelta):
        raise RephraseRequired("Please rephrase with a specific amount and date.")
    if delta.amount is None:
        raise RephraseRequired(NO_AMOUNT)
    if delta.start_day is None or delta.confidence is None:
        raise RephraseRequired("Please rephrase with a positive amount and a clear date or recurrence.")
    numbers =(delta.amount, delta.start_day, delta.confidence)
    if (not all(math.isfinite(x) for x in numbers) or delta.amount <= 0 or delta.start_day < 0
            or not 0.5 <= delta.confidence <= 1
            or (delta.cadence_days is not None and (not math.isfinite(delta.cadence_days) or delta.cadence_days <= 0))):
        raise RephraseRequired("Please rephrase with a positive amount and a clear date or recurrence.")
    if delta.kind == "one_time" and delta.cadence_days is not None:
        raise RephraseRequired("Please clarify whether this is a one-time or recurring expense.")
    return delta


def template(claims: list[Claim]) -> str:
    """Numbers and merchant names are formatted directly from typed claim values.

    Only explains the driver; readable_response adds the verdict and odds.
    """
    values = {claim.kind: claim.value for claim in claims}
    if "culprit_label" in values:
        return (f"Your biggest recurring pressure is {values['culprit_label']} "
                f"at {_money(float(values['culprit_amount']))} per payment. "
                "The replay shows how much it matters.")
    return "No single recurring bill is driving this."


def _money(value: float) -> str:
    return f"${value:,.0f}" if float(value).is_integer() else f"${value:,.2f}"


def _odds(rate: float) -> str:
    if 0 < rate < 0.01:
        return "under 1%"
    if 0.99 < rate < 1:
        return "over 99%"
    return f"{math.floor(rate * 100 + 0.5)}%"  # halves up, like the web's Math.round


def _when(day: date, today: date) -> str:
    offset = (day - today).days
    if offset == 0:
        return "today"
    if offset == 1:
        return "tomorrow"
    label = f"{day.strftime('%b')} {day.day}"
    return f"on {label}" if day.year == today.year else f"on {label}, {day.year}"


def _cadence(days: float) -> str:
    return {1: "a day", 7: "a week", 14: "every two weeks", 30: "a month",
            365: "a year"}.get(days, f"every {days:g} days")


def _horizon(weeks: int) -> str:
    if weeks % 52 == 0:
        years = weeks // 52
        return "year" if years == 1 else f"{years} years"
    return f"{weeks} weeks"


# A recurring charge must move the odds by at least this much to be called out.
# Mirrored in web/lib/summary.ts.
CULPRIT_MIN_CONTRIBUTION = 0.05
# Odds within one point of the baseline count as "no change" (simulation noise). Mirrored in web/lib/summary.ts.
NO_CHANGE = 0.01


def dip_summary(result: SimResult) -> dict | None:
    """When, how deep and how long the typical shortfall is, across paths that have one."""
    dipped = [p for p in result.paths if p.first_breach_week is not None]
    if not dipped:
        return None
    first = sorted(p.first_breach_week for p in dipped)[len(dipped) // 2]
    below = sorted(sum(b < result.breach_threshold for b in p.balances[1:]) for p in dipped)[len(dipped) // 2]
    final = sorted(p.balances[-1] for p in result.paths)[len(result.paths) // 2]
    return {"share": len(dipped) / len(result.paths), "first_week": first,
            "weeks_below": below, "recovers": final > result.breach_threshold}


def _weeks_or_months(weeks: int) -> str:
    if weeks <= 1:
        return "about a week"
    if weeks < 9:
        return f"about {weeks} weeks"
    months = max(2, math.floor(weeks / (52 / 12) + 0.5))
    return f"about {months} months"


def _dip_line(result: SimResult, today: date) -> str:
    dip = dip_summary(result)
    if dip is None or float(result.delta_breach_rate) <= float(result.baseline_breach_rate):
        return ""
    week_of = today + timedelta(weeks=dip["first_week"])
    when = f"the week of {week_of.strftime('%b')} {week_of.day}"
    if dip["share"] < 0.5:
        return f"If it does go wrong, the tight spot is most likely {when}."
    # Most paths dip here, so the all-path median low is the typical one and
    # matches the "typical lowest balance" shown on the replay and cards.
    # Rounds halves up (not Python's round-half-even) to match the web's Math.round.
    short = f"${math.floor(abs(result.median_min_balance - result.breach_threshold) + 0.5):,} short"
    below = f"Most likely you'd drop below {_money(result.breach_threshold)} {when}"
    if not dip["recovers"]:
        return f"{below} and not climb back within the next {_horizon(result.horizon_weeks)}, bottoming out around {short}."
    return (f"{below}, bottom out around {short}, and stay there for {_weeks_or_months(dip['weeks_below'])} "
            "before your paychecks catch up.")


def _money_per_month(item: DecisionDelta) -> float:
    return item.amount * (365 / 12) / item.cadence_days


def _term(years: float) -> str:
    months = round(years * 12)
    if months % 12 == 0:
        n = months // 12
        return "1 year" if n == 1 else f"{n} years"
    return "1 month" if months == 1 else f"{months} months"


# Labels that name nothing ("Expense") or are internal; the wording leaves them out.
_GENERIC_LABELS = {"expense", "purchase", "explicit expense", "test fixture expense", "habit spending", "income",
                   "one-time income", "extra income", "new income", "pay change", "rent ends", "loan", "afford"}
_ONE_TIME_INCOME = {"bonus", "gift", "refund", "prize", "tax refund", "payout"}
_PRONOUNS = {"it", "this", "that", "them", "these", "those", "one", "something", "stuff", "thing"}


def noun(label: str | None, raw_text: str = "") -> str | None:
    """A short label as it should read mid-sentence ("the iPhone 18 Pro", "the house"), or None if it names nothing.

    Uses the person's own casing when the label appears in what they wrote; otherwise lowercases a plain word
    but keeps names like "iPhone" or "DoorDash".
    """
    label = (label or "").strip()
    if (not label or label.lower() in _GENERIC_LABELS or label.lower() in _PRONOUNS or len(label) > 30
            or len(label.split()) > 4
            or any(c in label for c in "$?/") or label.lower().endswith((" payment", " proceeds"))):
        return None
    at = raw_text.lower().find(label.lower())
    if at >= 0:
        return raw_text[at:at + len(label)]
    return label if len(label) > 1 and label[1].isupper() else label[0].lower() + label[1:]


def _join(phrases: list[str]) -> str:
    """"A and B", or "A, B, and C"; a comma also separates two parts that each contain "and"."""
    if len(phrases) <= 1:
        return "".join(phrases)
    if len(phrases) == 2 and not any(" and " in p for p in phrases):
        return " and ".join(phrases)
    return ", ".join(phrases[:-1]) + f", and {phrases[-1]}"


def _item_phrase(item: DecisionDelta, today: date, raw_text: str = "") -> str:
    when = _when(today + timedelta(days=item.start_day), today)
    thing = noun(item.label, raw_text)
    if item.kind == "recurring":
        starting = "starting today" if when == "today" else f"starting {when.removeprefix('on ')}"
        phrase = f"adding {_money(item.amount)} {_cadence(item.cadence_days)}"
        # "for Netflix" (a brand) but "for the gym membership"
        phrase += (f" for {thing}" if thing[0].isupper() else f" for the {thing}") if thing else ""
        phrase += f" {starting}"
        if item.end_day is not None:
            months = round((item.end_day - item.start_day) / 30)
            phrase += f" for {months} month{'s' if months != 1 else ''}"
        return phrase
    return f"spending {_money(item.amount)}{f' on the {thing}' if thing else ''} {when}"


def _purchases_phrase(items: list[DecisionDelta], today: date, raw_text: str) -> str:
    """Same-day purchases as one: "spending $639 today on the PS5 ($499) and the games ($140)"."""
    when = _when(today + timedelta(days=items[0].start_day), today)
    total = _money(sum(it.amount for it in items))
    things = [noun(it.label, raw_text) for it in items]
    if not all(things):
        return f"spending {total} {when}"
    parts = [f"the {t} ({_money(it.amount)})" for t, it in zip(things, items)]
    return f"spending {total} {when} on {_join(parts)}"


def _income_phrase(item: DecisionDelta, today: date) -> str:
    if item.kind == "one_time":  # a bonus, gift or refund
        when = _when(today + timedelta(days=item.start_day), today)
        kind = item.label.lower()
        return f"getting a {_money(item.amount)} {kind} {when}" if kind in _ONE_TIME_INCOME else f"getting {_money(item.amount)} {when}"
    more = item.direction == "inflow"
    return f"earning about {_money(math.floor(_money_per_month(item) + 0.5))} {'more' if more else 'less'} a month"


def _loan_phrase(loan: dict, today: date, raw_text: str) -> str:
    when = _when(today + timedelta(days=loan.get("start_day", 0)), today)
    if loan["rate_pct"] is None:  # the user gave the payment, not a rate
        terms = f"paying {_money(loan['monthly_payment'])} a month for {_term(loan['years'])}"
    else:
        terms = (f"at {loan['rate_pct']:g}% over {_term(loan['years'])} "
                 f"(about {_money(math.floor(loan['monthly_payment'] + 0.5))} a month)")
    down = loan["down_payment"]
    thing = noun(loan.get("label"), raw_text)
    if thing:
        text = f"buying the {thing} with {f'{_money(down)} down and ' if down > 0 else ''}a {_money(loan['principal'])} loan {terms}"
    elif down > 0:
        text = f"borrowing {_money(loan['principal'])} {terms} with {_money(down)} down"
    else:
        text = f"borrowing {_money(loan['principal'])} {terms}"
    return text if when == "today" else f"{text} {when}"


def describe_decision(decision: Decision | DecisionDelta, today: date) -> str:
    """Plain-language phrase for the whole decision. Every number comes from parsed inputs or code math."""
    decision = as_decision(decision)
    raw = decision.raw_text or (decision.items[0].raw_text if decision.items else "")
    loan_idx = {i for loan in decision.terms.get("loans", []) for i in loan["item_indexes"]}
    phrases = []
    shown = [item for idx, item in enumerate(decision.items)
             if idx not in loan_idx and item.amount != 0 and item.label != "Rent ends"]
    spends = [it for it in shown if it.kind == "one_time" and it.direction == "outflow"]
    together = len(spends) > 1 and len({it.start_day for it in spends}) == 1  # several purchases on one day
    for item in shown:
        if item.direction == "inflow" or item.label == "Pay change":
            phrases.append(_income_phrase(item, today))
        elif together and item in spends:
            if item is spends[0]:
                phrases.append(_purchases_phrase(spends, today, raw))
        else:
            phrases.append(_item_phrase(item, today, raw))
    phrases += [_loan_phrase(loan, today, raw) for loan in decision.terms.get("loans", [])]
    text = _join(phrases) or "making this change"
    return text[0].upper() + text[1:]


def adds_income(decision: Decision | DecisionDelta) -> bool:
    """True when the decision brings money in (a raise, a bonus), not just a cost or a loan's cash."""
    decision = as_decision(decision)
    loan_idx = {i for loan in decision.terms.get("loans", []) for i in loan["item_indexes"]}
    return any(item.direction == "inflow" and idx not in loan_idx and item.label != "Rent ends"
               for idx, item in enumerate(decision.items))


def decision_notes(decision: Decision | DecisionDelta) -> str:
    terms = as_decision(decision).terms
    notes = []
    for loan in terms.get("assumed_loans", []):
        parts = []
        if "rate_pct" in loan:
            parts.append(f"your typical {loan['rate_pct']:g}% {loan['kind']} rate")
        if "years" in loan:
            parts.append(f"a {loan['years']}-year term")
        notes.append(f"You didn't give a {'rate' if 'rate_pct' in loan else 'term'}"
                     f"{' or term' if len(parts) == 2 else ''} for the {loan['label'].lower()}, "
                     f"so this assumes {' and '.join(parts)}.")
    if "income_tax_assumed" in terms.get("assumptions", []):
        notes.append(f"Assumes about {round((1 - terms['net_factor']) * 100)}% of that income goes to taxes.")
    if "rent_ends" in terms.get("assumptions", []):
        notes.append(f"Assumes your {_money(terms['rent_ended']['amount'])} rent stops once you buy. "
                     "Taxes, insurance and upkeep on the home are not included.")
    return " ".join(notes)


def _month_year(day: date) -> str:
    return f"{day.strftime('%b')} {day.year}"


def financing_line(result: FinancingResult | None, raw_text: str = "", threshold: float = 0.0) -> str:
    """A fixed-rate loan that would keep the odds safe, or an honest note that none of the listed terms does."""
    if result is None:
        return ""
    thing = noun(result.label, raw_text)
    what = f"the {thing}" if thing else "this purchase"
    if result.option is None:
        return (f"Financing {what} at your typical {result.apr * 100:g}% APR wouldn't make it safe "
                "on any term up to 5 years.")
    o = result.option
    term = f"{o.months} months" if o.months < 24 else f"{o.months // 12} years" if o.months % 12 == 0 else f"{o.months} months"
    return (f"A loan could work: financing {what} ({_money(o.amount)}) at your typical {o.apr * 100:g}% APR over "
            f"{term} is about {_money(round(o.monthly_payment))} a month ({_money(round(o.total_interest))} in interest "
            f"overall) and keeps the odds of dipping below {_money(threshold)} at about {_odds(o.breach_rate)}.")


def outlook_line(outlook: Outlook | None, today: date, decision: Decision | DecisionDelta | None = None,
                 threshold: float = 0.0) -> str:
    """When the same decision would be safe, from the engine's delayed-start search."""
    if outlook is None:
        return ""
    if outlook.feasible:
        start = today + timedelta(days=outlook.first_start_day, weeks=outlook.wait_weeks)
        when = f"{start.strftime('%b')} {start.day}" if outlook.wait_weeks < 9 else _month_year(start)
        waited = ("1 week" if outlook.wait_weeks == 1 else f"{outlook.wait_weeks} weeks" if outlook.wait_weeks < 9
                  else f"about {round(outlook.wait_weeks / 52 * 12)} months")
        return (f"Waiting would help: starting around {when} ({waited} later than you asked) brings the odds of "
                f"dipping below {_money(threshold)} down to about {_odds(outlook.breach_rate)}.")
    years = _horizon(outlook.searched_weeks).replace("156 weeks", "3 years")
    line = f"Waiting up to {years} wouldn't make this safe."
    gap = outlook.monthly_gap or 0
    if gap >= 10:
        gap_text = _money(round(gap, -1))
        pays = decision is not None and any(it.kind == "recurring" and it.direction == "outflow" and it.amount > 0
                                            for it in as_decision(decision).items)
        line += (f" Even then, the payments would leave you about {gap_text} a month short, so it comes down to "
                 "monthly income, not timing." if pays else
                 f" Your regular spending already runs about {gap_text} a month ahead of your income, so it comes "
                 "down to monthly income, not timing.")
    return line


def readable_response(result: SimResult, delta: Decision | DecisionDelta, prose: str, today: date,
                      outlook: Outlook | None = None, financing: FinancingResult | None = None) -> str:
    """Add context directly from checked simulation outputs and parsed inputs.

    Keep this outside the LLM and its claims-only cache: two questions can
    produce identical claims while having different amounts or payment dates.
    """
    rate = float(result.delta_breach_rate)
    base = float(result.baseline_breach_rate)
    what = describe_decision(delta, today)

    # Tiers match web/lib/verdict.ts (Sustainable < 20%, Worth a look < 50%).
    if rate == 0:
        lead = f"{what} looks safe. Your balance stayed above {_money(result.breach_threshold)} in every simulation."
    elif adds_income(delta) and rate < base - NO_CHANGE:
        lead = f"{what} would lower your odds of running short." + (
            " Your cash is still tight either way." if rate >= 0.5 else "")
    elif rate <= base + NO_CHANGE:
        lead = ("Your cash is already tight, with or without this."
                if base >= 0.5 else f"{what} doesn't really change your odds.")
    elif rate < 0.2:
        lead = f"{what} looks manageable."
    elif rate < 0.5:
        lead = f"{what} is doable, but worth a second look."
    else:
        lead = f"Heads up: {what[0].lower() + what[1:]} would be a real stretch."

    odds = (f"Chance your balance dips below {_money(result.breach_threshold)} "
            f"in the next {_horizon(result.horizon_weeks)}:\n"
            f"With it: {_odds(rate)}\n"
            f"Without it: {_odds(base)}")
    footer = f"This is a forecast from {result.n_paths} simulations, not a guarantee."
    # A contributor that barely moves the odds is noise next to the verdict.
    ended = as_decision(delta).terms.get("rent_ended", {}).get("label")  # rent stops, so it isn't the squeeze
    if (result.culprit is None or result.culprit.marginal_contribution < CULPRIT_MIN_CONTRIBUTION
            or result.culprit.label == ended):
        prose = ""
    raw = as_decision(delta).raw_text
    parts = [lead, _dip_line(result, today), odds, outlook_line(outlook, today, delta, result.breach_threshold),
             financing_line(financing, raw, result.breach_threshold), prose.strip(), decision_notes(delta), footer]
    return "\n\n".join(part for part in parts if part)


def check_evidence(result: SimResult, resolve: Callable) -> None:
    if not result.claims:
        raise ValueError("Simulation emitted no evidenced claims")
    for claim in result.claims:
        if not claim.evidence:
            raise ValueError("Claim has no transaction evidence")
        for tid in claim.evidence:
            resolve(tid)


def checked_template(result: SimResult) -> str:
    """Fallback validation is construction-based; arbitrary prose never enters it."""
    values = {claim.kind: claim for claim in result.claims}
    expected = {"baseline_breach_rate": result.baseline_breach_rate,
                "breach_rate_with_delta": result.delta_breach_rate,
                "paths_simulated": result.n_paths}
    if result.culprit:
        expected.update(culprit_label=result.culprit.label, culprit_amount=result.culprit.amount,
                        culprit_marginal_contribution=result.culprit.marginal_contribution)
    elif "culprit_label" in values:
        raise ValueError("Unexpected culprit claim")
    for kind, value in expected.items():
        if kind not in values or values[kind].value != value:
            raise ValueError("Claim/result mismatch")
        if isinstance(value, (int, float)) and not math.isfinite(value):
            raise ValueError("Non-finite claim")
    return template(result.claims)


def validation_passed(value) -> bool:
    return (value.get("passed") if isinstance(value, dict) else getattr(value, "passed", None)) is True


@dataclass
class Pipeline:
    parser: Callable
    renderer: Callable = render
    validator: Callable = validate
    simulator: Callable = run
