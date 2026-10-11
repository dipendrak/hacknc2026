"""Plain-English message: wording shared with web/lib/summary.ts, plus the
parse/validate/render behavior behind it."""
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from api.pipeline import RephraseRequired, _odds, readable_response, validate_delta
from core.model import Claim, Culprit, DecisionDelta

CASES = json.loads((Path(__file__).resolve().parents[2] / "web" / "tests" / "summary-cases.json").read_text())
TODAY = date.fromisoformat(CASES["today"])


def make_result(spec, culprit=None):
    paths = [SimpleNamespace(balances=b, min_balance=min(b), first_breach_week=fb) for b, fb in spec["paths"]]
    return SimpleNamespace(
        n_paths=len(paths), horizon_weeks=len(paths[0].balances) - 1, breach_threshold=0.0,
        baseline_breach_rate=spec["baseline_breach_rate"], delta_breach_rate=spec["delta_breach_rate"],
        median_min_balance=spec["median_min_balance"], culprit=culprit, paths=paths)


def make_delta(spec):
    return DecisionDelta(spec["kind"], spec["amount"], spec["start_day"], spec["cadence_days"])


@pytest.mark.parametrize("rate,expected", CASES["odds"])
def test_odds_match_web(rate, expected):
    assert _odds(rate) == expected


@pytest.mark.parametrize("case", CASES["cases"], ids=lambda c: c["name"])
def test_verdict_and_dip_match_web(case):
    prose = readable_response(make_result(case["result"]), make_delta(case["delta"]), "", TODAY)
    paragraphs = prose.split("\n\n")
    assert paragraphs[0] == case["verdict"]
    if case["dip"] is None:
        assert paragraphs[1].startswith("Chance your balance dips below $0")
    else:
        assert paragraphs[1] == case["dip"]


def test_message_layout_and_odds_block():
    case = CASES["cases"][0]
    prose = readable_response(make_result(case["result"]), make_delta(case["delta"]), "", TODAY)
    assert "Chance your balance dips below $0 in the next 4 weeks:\nWith it: 100%\nWithout it: 4%" in prose
    assert prose.endswith("This is a forecast from 5 simulations, not a guarantee.")
    for jargon in ("breach", "delta", "culprit", "percentile", "threshold"):
        assert jargon not in prose.lower()


@pytest.mark.parametrize("contribution,shown", [(0.028, False), (0.05, True), (0.2, True)])
def test_driver_sentence_only_when_it_moves_the_odds(contribution, shown):
    case = CASES["cases"][0]
    culprit = Culprit("rec_1", "DoorDash", 57.97, 0.9, contribution)
    prose = readable_response(make_result(case["result"], culprit), make_delta(case["delta"]),
                              "DoorDash at $57.97 per payment is what squeezes you most.", TODAY)
    assert ("DoorDash" in prose) is shown


def test_parser_defaults_a_missing_date_to_today():
    from llm.parse import _parse_heuristic
    delta = _parse_heuristic("Should I sign up for a $60 a month gym membership?", TODAY)
    assert (delta.kind, delta.amount, delta.cadence_days, delta.start_day) == ("recurring", 60, 30, 0)
    assert validate_delta(delta) is delta


def test_missing_amount_asks_for_one():
    from llm.parse import _parse_heuristic
    with pytest.raises(RephraseRequired, match="Please include a dollar amount"):
        validate_delta(_parse_heuristic("should I buy a car", TODAY))


def test_renderer_skips_gemini_without_a_contributor(monkeypatch):
    import llm.render as render
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    def no_call(*args, **kwargs):
        raise AssertionError("Gemini should not be called")
    monkeypatch.setattr(render, "generate_text", no_call)
    assert render.render([], None) == "No single recurring bill stands out as the cause."


def test_renderer_only_shows_gemini_the_contributor(monkeypatch):
    import llm.render as render
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    prompts = []
    def capture(prompt, **kwargs):
        prompts.append(prompt)
        return "DoorDash at $57.97 per payment is what squeezes you most."
    monkeypatch.setattr(render, "generate_text", capture)
    claims = [Claim("a", "baseline_breach_rate", 0.036, "probability", ["t"]),
              Claim("b", "breach_rate_with_delta", 0.992, "probability", ["t"]),
              Claim("c", "median_min_balance", -798.5, "usd", ["t"]),
              Claim("d", "culprit_label", "DoorDash", None, ["t"]),
              Claim("e", "culprit_amount", 57.97, "usd", ["t"])]
    culprit = Culprit("rec_1", "DoorDash", 57.97, 0.9, 0.2)
    assert render.render(claims, culprit).startswith("DoorDash")
    claim_lines = prompts[0].split("Claims (the ONLY source of numbers in your response):")[1].split("CRITICAL RULES")[0]
    assert "culprit_label: DoorDash" in claim_lines and "culprit_amount: $57.97" in claim_lines
    assert "breach_rate" not in claim_lines and "median_min_balance" not in claim_lines


def test_multi_item_decision_wording_matches_the_web():
    from api.pipeline import decision_notes, describe_decision
    from core.model import Decision
    for case in CASES["decision_cases"]:
        decision = Decision.from_dict(case["decision"])
        assert describe_decision(decision, TODAY) == case["phrase"], case["name"]
        assert (decision_notes(decision) or None) == case["note"], case["name"]


# --- wording review ------------------------------------------------------------------------------

def _bonus_and_phone():
    from llm import parse_complex as pc
    return pc.parse_complex("If I made $3000 off of a bonus, could I buy a $1999 iPhone 18 Pro", TODAY, use_llm=False)


def test_income_that_lowers_the_odds_says_so():
    decision = _bonus_and_phone()
    tight = readable_response(make_result({**CASES["cases"][0]["result"], "baseline_breach_rate": 0.99,
                                           "delta_breach_rate": 0.88}), decision, "", TODAY)
    assert tight.startswith("Getting a $2,340 bonus today and spending $1,999 on the iPhone 18 Pro today would "
                            "lower your odds of running short. Your cash is still tight either way.")
    calm = readable_response(make_result({**CASES["cases"][0]["result"], "baseline_breach_rate": 0.09,
                                          "delta_breach_rate": 0.04}), decision, "", TODAY)
    assert "would lower your odds of running short.\n" in calm and "still tight" not in calm


def test_a_plain_purchase_never_claims_to_lower_the_odds():
    result = make_result({**CASES["cases"][0]["result"], "baseline_breach_rate": 0.65, "delta_breach_rate": 0.6})
    lead = readable_response(result, make_delta(CASES["cases"][0]["delta"]), "", TODAY).split("\n\n")[0]
    assert lead == "Your cash is already tight, with or without this."  # noise, not a real improvement


def test_waiting_and_financing_lines_read_naturally():
    from core.feasible import Outlook
    from core.financing import FinancingOption, FinancingResult
    from api.pipeline import financing_line, outlook_line
    soon = outlook_line(Outlook(True, 2, 0.08, None, 156, 51), TODAY)
    assert soon == ("Waiting would help: starting around Dec 14 (2 weeks later than you asked) brings the odds "
                    "of dipping below $0 down to about 8%.")
    one_time = outlook_line(Outlook(False, None, None, 21.0, 156), TODAY, _bonus_and_phone())
    assert "Your regular spending already runs about $20 a month ahead of your income" in one_time
    assert "payments" not in one_time  # a one-time purchase has no payments
    assert outlook_line(Outlook(False, None, None, 3.0, 156), TODAY) == "Waiting up to 3 years wouldn't make this safe."
    ok = financing_line(FinancingResult("Downpayment", 2000, 0.1475, FinancingOption("Downpayment", 2000, 0.1475, 12,
                                                                                    180.2, 162.6, 0.09)),
                        "Can I afford a 2000 dollar downpayment around next weekend")
    assert ok == ("A loan could work: financing the downpayment ($2,000) at your typical 14.75% APR over 12 months is "
                  "about $180 a month ($163 in interest overall) and keeps the odds of dipping below $0 at about 9%.")
    none = financing_line(FinancingResult("iPhone 18 Pro", 1999, 0.105, None), "a $1999 iPhone 18 Pro")
    assert none == "Financing the iPhone 18 Pro at your typical 10.5% APR wouldn't make it safe on any term up to 5 years."


def test_a_loans_own_down_payment_is_never_financed():
    from core.financing import financeable_index
    from llm import parse_complex as pc
    house = pc.parse_complex("can I afford a $400k house with 20% down", TODAY, use_llm=False)
    assert financeable_index(house) is None
    phone = _bonus_and_phone()
    assert phone.items[financeable_index(phone)].label == "iPhone 18 Pro"


def test_no_amount_message_explains_what_to_ask():
    from api.pipeline import NO_AMOUNT
    assert NO_AMOUNT.startswith("Please include a dollar amount") and "$400 phone" in NO_AMOUNT


def test_brands_take_no_article_and_pronouns_are_not_names():
    from api.pipeline import describe_decision, noun
    from llm import parse_complex as pc
    netflix = pc.parse_complex("If I were to get Netflix for $20 a month starting today, would I be fine?", TODAY,
                               use_llm=False)
    assert describe_decision(netflix, TODAY) == "Adding $20 a month for Netflix starting today"
    assert noun("It", "financed 2000 of it") is None
