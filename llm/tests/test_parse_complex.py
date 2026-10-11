from datetime import date
from unittest import mock
from unittest.mock import patch

import pytest

from llm import parse_complex as pc

TODAY = date(2026, 10, 10)


def parse(text, **kw):
    return pc.parse_complex(text, TODAY, use_llm=False, **kw)


def test_house_with_job_builds_income_down_payment_and_mortgage():
    d = parse("I just got a job making $90k a year, can I afford a $350k house with 20% down at 7% over 30 years?")
    income, down, payment = d.items
    assert income.direction == "inflow" and income.cadence_days == pytest.approx(365 / 12)
    assert income.amount == pytest.approx(90_000 / 12 * 0.78, abs=0.01)  # disclosed net haircut
    assert down.kind == "one_time" and down.amount == 70_000
    assert payment.cadence_days == 30 and payment.end_day == 360 * 30
    assert payment.amount == pytest.approx(1862.85, abs=0.01)  # $280k @ 7% / 30y
    assert d.terms["replace_income"][0]["annual"] == pytest.approx(70_200)


def test_one_year_loan_ends_after_twelve_payments():
    d = parse("Can I afford a $5,000 loan at 6% for 1 year?")
    proceeds, payment = d.items  # a personal loan's cash lands in the account first
    assert proceeds.direction == "inflow" and proceeds.amount == 5000
    assert payment.amount == pytest.approx(430.33, abs=0.01) and payment.end_day == 360


def test_raise_is_additive_inflow_and_rent_is_monthly_outflow():
    d = parse("I got a $500/month raise, can I afford $1,200/month rent?")
    raise_, rent = d.items
    assert raise_.direction == "inflow" and "replace_income" not in d.terms
    assert rent.direction == "outflow" and rent.amount == 1200 and rent.cadence_days == 30


def test_nearest_cue_wins_in_one_sentence():
    d = parse("I got a $400/month raise and want a $28k car with $4k down at 5.5% for 5 years")
    # raise, down payment, payment: a car loan pays the dealer, so no proceeds land in the account
    assert [i.direction for i in d.items] == ["inflow", "outflow", "outflow"]
    assert d.items[1].amount == 4000 and d.terms["loans"][0]["principal"] == 24000
    assert [i.label for i in d.items[1:]] == ["Car down payment", "Car payment"]


def test_timing_and_fixed_length_expense():
    d = parse("can I afford $400 a month for 12 months starting in 30 days")
    (item,) = d.items
    assert item.start_day == 30 and item.end_day == 30 + 12 * 30


def test_thousands_commas_do_not_split_the_clause():
    d = parse("Can I afford a $5,000 loan at 6% for 1 year?")
    assert d.terms["loans"][0]["price"] == 5000


@pytest.mark.parametrize("text", [
    "can I afford a $30k car at 7%",                    # a car loan's term is never guessed
    "can I afford $400 and also $90 at 3% somewhere",  # stray percentage
    "I make $50 an hour, can I afford $400?",           # hourly pay unsupported
])
def test_unaccounted_numbers_are_refused_not_dropped(text):
    with pytest.raises(pc.ComplexParseError):
        parse(text)


def test_plain_single_expense_still_takes_the_legacy_path():
    assert not pc.needs_complex("Can I afford $400 today?")
    assert not pc.needs_complex("Can I afford $1,400 on December 5th?")
    assert pc.needs_complex("Can I afford $65/month starting in 30 days?")
    assert pc.needs_complex("I got a raise, can I afford $400?")


def _stub_llm(payload):
    return mock.patch.dict("os.environ", {"GEMINI_API_KEY": "x"}), mock.patch(
        "llm.gemini.generate_text", return_value=payload)


def test_llm_fallback_accepts_faithful_extraction():
    text = "my sister is lending me some money then I owe $300 each month for 5 years at 4%"
    payload = ('{"income": [], "expenses": [], "loans": [{"price": 300, "down": null, "down_pct": null, '
               '"rate_pct": 4, "years": 5, "start_day": 0, "label": "loan"}]}')
    env, gen = _stub_llm(payload)
    with env, gen:
        d = pc.parse_complex(text, TODAY)
    assert d.confidence == 0.7  # capped when the LLM helped
    assert d.terms["loans"][0]["monthly_payment"] == pytest.approx(5.52, abs=0.01)  # code did the math


def test_llm_fallback_rejects_invented_or_dropped_numbers():
    text = "my sister is lending me a bit then I owe $300 at 4% for 5 years"
    invented = ('{"income": [], "expenses": [], "loans": [{"price": 12000, "down": null, "down_pct": null, '
                '"rate_pct": 4, "years": 5, "start_day": 0, "label": "loan"}]}')
    env, gen = _stub_llm(invented)
    with env, gen, pytest.raises(pc.ComplexParseError):
        pc.parse_complex(text, TODAY)


def test_llm_unavailable_gives_a_rephrase_hint(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(pc.ComplexParseError) as exc:
        pc.parse_complex("buy something for $400 at 3%", TODAY)
    assert "Try one clear sentence" in str(exc.value)


def test_two_loans_one_with_stated_payment_and_bare_numbers():
    d = parse("I make 3000 a month from my new job but want to get a 300000 house with 30 year mortgage "
              "7 percent yearly interest with 6000 dollar down and I take a 3000 dollar loan with "
              "300 dollar monthly payment over 1 year")
    income, down, mortgage, proceeds, loan = d.items  # mortgage cash goes to the seller; the personal loan's to you
    assert proceeds.direction == "inflow" and proceeds.amount == 3000
    assert income.direction == "inflow" and income.amount == pytest.approx(3000 * 0.78)
    assert down.amount == 6000 and down.kind == "one_time"
    assert mortgage.amount == pytest.approx(1955.99, abs=0.01) and mortgage.end_day == 360 * 30
    assert loan.amount == 300 and loan.end_day == 12 * 30  # stated payment used as-is, no rate invented
    assert d.terms["loans"][1]["rate_pct"] is None
    assert d.terms["replace_income"][0]["annual"] == pytest.approx(3000 * 12 * 0.78)


def test_stated_payment_that_cannot_repay_the_loan_is_refused():
    with pytest.raises(pc.ComplexParseError):
        parse("can I take a $3000 loan with a $100 monthly payment over 1 year")


# --- bonuses, product names, quantities, labels -------------------------------------------------

def test_one_time_bonus_and_model_number_are_not_expenses():
    d = parse("If I made $3000 off of a bonus, could I buy a $1999 iPhone 18 Pro")
    bonus, phone = d.items
    assert (bonus.kind, bonus.direction, bonus.label) == ("one_time", "inflow", "Bonus")
    assert bonus.amount == pytest.approx(3000 * 0.78)  # same after-tax assumption as pay
    assert (phone.kind, phone.direction, phone.amount, phone.label) == ("one_time", "outflow", 1999, "iPhone 18 Pro")
    assert "income_tax_assumed" in d.terms["assumptions"]


@pytest.mark.parametrize("text,kind", [
    ("a $1999 iPhone 18 Pro", "name"), ("a PS5 for $499", "name"), ("Galaxy S24 for $800", "name"),
    ("get a 300000 house", "money"), ("rent 1200 a month", "money"), ("spend 300 today", "money"),
])
def test_model_numbers_are_names_but_amounts_stay_amounts(text, kind):
    bare = [t for t in pc.tokenize(text) if not t["dollar"]]
    assert bare[0]["kind"] == kind


def test_quantities_multiply_the_price():
    d = parse("thinking about a PS5 for $499 and 2 games at $70 each")
    ps5, games = d.items
    assert (ps5.amount, ps5.label) == (499, "PS5")
    assert (games.amount, games.label) == (140, "Games")


@pytest.mark.parametrize("raw,expected", [
    ("iPhone 18 Pro", "iPhone 18 Pro"), ("rent", "Rent"), ("the car", "Car"),
    ("If I made $3000 off of a bonus", "Expense"), ("could I? buy it", "Expense"), ("x" * 41, "Expense"), (None, "Expense"),
])
def test_labels_are_short_and_display_safe(raw, expected):
    assert pc.clean_label(raw, "Expense") == expected


def test_personal_loan_proceeds_label_does_not_repeat_loan():
    d = parse("Can I afford a $5,000 loan at 6% for 1 year?")
    assert [i.label for i in d.items] == ["Loan proceeds", "Loan payment"]


# --- Gemini first ------------------------------------------------------------------------------

IPHONE = "If I made $3000 off of a bonus, could I buy a $1999 iPhone 18 Pro"
IPHONE_JSON = ('{"income": [{"amount": 3000, "period": "once", "replaces": false, "net": false, '
               '"start_date": "2026-10-10", "label": "Bonus"}], "expenses": [{"amount": 1999, "quantity": null, '
               '"cadence": "once", "months": null, "start_date": "2026-10-17", "label": "iPhone 18 Pro"}], '
               '"loans": [], "ignored": [18]}')


def test_gemini_fills_fields_and_code_converts_dates():
    env, gen = _stub_llm(IPHONE_JSON)
    with env, gen:
        d = pc.parse_with_gemini(IPHONE, TODAY)
    bonus, phone = d.items
    assert bonus.direction == "inflow" and phone.label == "iPhone 18 Pro"
    assert phone.start_day == 7  # 2026-10-17 is 7 days after TODAY; Gemini gave a date, code did the math


def test_gemini_cannot_drop_or_ignore_a_dollar_amount():
    dropped = IPHONE_JSON.replace('"ignored": [18]', '"ignored": [18, 3000]').replace(
        '[{"amount": 3000, "period": "once", "replaces": false, "net": false, '
        '"start_date": "2026-10-10", "label": "Bonus"}]', "[]")
    env, gen = _stub_llm(dropped)
    with env, gen, pytest.raises(pc.ComplexParseError):
        pc.parse_with_gemini(IPHONE, TODAY)


def test_gemini_may_not_invent_numbers():
    invented = IPHONE_JSON.replace('"amount": 1999', '"amount": 1799')
    env, gen = _stub_llm(invented)
    with env, gen, pytest.raises(pc.ComplexParseError):
        pc.parse_with_gemini(IPHONE, TODAY)


def test_no_amount_skips_gemini_entirely():
    env, gen = _stub_llm("{}")
    with env, gen as called, pytest.raises(pc.NotAFinanceQuestion):
        pc.parse_with_gemini("should I buy a car", TODAY)
    called.assert_not_called()


def test_gemini_item_without_an_amount_is_refused():
    env, gen = _stub_llm('{"income": [], "expenses": [{"amount": null, "cadence": "once", "label": "Car"}], '
                         '"loans": [], "ignored": []}')
    with env, gen, pytest.raises(pc.ComplexParseError):
        pc.parse_with_gemini("should I buy a car for $", TODAY)


# --- natural-language robustness: number words, qualifiers, relative timing -------------------------------

from datetime import date as _date

HOUSE = ("What if I make 3000 net a month from my new job but want to get a 300000 house with 30 year mortgage "
         "7 percent yearly interest with 6000 dollar down and I take a 3000 dollar loan with 300 dollar monthly "
         "payment over 1 year and I put the down payment down halfway into the first year?")


def test_normalize_number_words_and_scales():
    assert pc.normalize_text("a thirty year mortgage") == "a 30 year mortgage"
    assert pc.normalize_text("in six months") == "in 6 months"
    assert pc.normalize_text("a 3 grand loan over a year") == "a 3000 loan over 1 year"
    assert pc.normalize_text("a year from now") == "1 year from now"


def test_net_a_month_reads_as_a_monthly_income():
    d = pc.parse_complex("What if I make 5000 net a month from my new job and buy a 300000 house with 30 year "
                         "mortgage 7 percent with 6000 down in 6 months?", TODAY, use_llm=False)
    income = d.items[0]
    assert income.amount == 5000 and income.direction == "inflow" and d.terms["replace_income"]
    assert d.terms["loans"][0]["start_day"] == (pc._add_months(TODAY, 6) - TODAY).days


def test_halfway_into_the_first_year_is_six_months_out():
    d = pc.parse_complex(HOUSE, TODAY, use_llm=False)
    starts = {i.label: i.start_day for i in d.items}
    assert starts["House down payment"] == starts["House payment"] == (pc._add_months(TODAY, 6) - TODAY).days
    assert starts["New income"] == 0  # the new job starts now; only the purchase waits


def test_gemini_relative_start_is_resolved_by_code():
    payload = ('{"income": [], "expenses": [], "loans": [{"price": 20000, "down": null, "down_pct": 10, "rate_pct": 6, '
               '"payment": null, "years": 5, "start": {"in": 3, "unit": "months"}, "label": "Car"}], "ignored": [3]}')
    env, gen = _stub_llm(payload)
    with env, gen:
        d = pc.parse_with_gemini("I want a $20000 car 10% down at 6% for 5 years in 3 months", TODAY)
    assert d.terms["loans"][0]["start_day"] == (pc._add_months(TODAY, 3) - TODAY).days


def test_dropped_timing_is_sent_back_for_a_fix():
    text = "I want a $20000 car 10% down at 6% for 5 years in 3 months"
    wrong = ('{"income": [], "expenses": [], "loans": [{"price": 20000, "down_pct": 10, "rate_pct": 6, "years": 5, '
             '"start": {"in": 0, "unit": "days"}, "label": "Car"}], "ignored": [3]}')
    fixed = wrong.replace('"in": 0, "unit": "days"', '"in": 3, "unit": "months"')
    with patch.dict("os.environ", {"GEMINI_API_KEY": "x"}), patch("llm.gemini.generate_text", side_effect=[wrong, fixed]) as gen:
        d = pc.parse_with_gemini(text, TODAY)
    assert gen.call_count == 2 and "rejected" in gen.call_args_list[1][0][0]
    assert d.terms["loans"][0]["start_day"] > 80


def test_still_wrong_after_correction_falls_back_to_the_words():
    text = "I want a $20000 car 10% down at 6% for 5 years in 3 months"
    wrong = ('{"income": [], "expenses": [], "loans": [{"price": 20000, "down_pct": 10, "rate_pct": 6, "years": 5, '
             '"start": {"in": 0, "unit": "days"}, "label": "Car"}], "ignored": [3]}')
    with patch.dict("os.environ", {"GEMINI_API_KEY": "x"}), patch("llm.gemini.generate_text", return_value=wrong):
        d = pc.parse_with_gemini(text, TODAY)
    assert d.terms["loans"][0]["start_day"] > 80


def test_loan_for_a_purchase_pays_the_seller_but_plain_borrowing_adds_cash():
    car = pc.parse_complex("new job pays 85k, want a $30k car with 10% down at 6% for 5 years", TODAY, use_llm=False)
    assert not any(i.direction == "inflow" and "proceeds" in i.label.lower() for i in car.items)
    cash = pc.parse_complex(HOUSE, TODAY, use_llm=False)
    assert [i.amount for i in cash.items if "proceeds" in i.label.lower()] == [3000.0]


# --- missing loan rate / term come from the person's own settings ------------------------------

def test_home_loan_without_term_assumes_30_years():
    d = parse("can I afford a $350k house at 7%")
    assert d.terms["loans"][0]["years"] == 30 and d.terms["loans"][0]["rate_pct"] == 7
    assert d.terms["assumed_loans"] == [{"label": "House", "years": 30}]


def test_home_loan_without_rate_uses_the_persons_mortgage_rate():
    d = pc.parse_complex("can I afford a $400k house with 20% down", TODAY, use_llm=False, mortgage_apr=0.07375)
    loan = d.terms["loans"][0]
    assert (loan["rate_pct"], loan["years"], loan["down_payment"]) == (7.375, 30, 80_000)
    assert d.terms["assumed_loans"] == [{"label": "House", "years": 30, "rate_pct": 7.375, "kind": "mortgage"}]


def test_personal_loan_without_rate_uses_the_persons_loan_rate():
    d = pc.parse_complex("can I take a $5,000 loan for 2 years", TODAY, use_llm=False, personal_loan_apr=0.1475)
    loan = d.terms["loans"][0]
    assert loan["rate_pct"] == 14.75 and loan["years"] == 2
    assert d.terms["assumed_loans"][0]["kind"] == "personal loan"


def test_stated_rate_always_wins_over_the_persons_rate():
    d = pc.parse_complex("can I afford a $400k house with 20% down at 6% over 30 years", TODAY,
                         use_llm=False, mortgage_apr=0.0775)
    assert d.terms["loans"][0]["rate_pct"] == 6 and "assumed_loans" not in d.terms


# --- "financed X of it" -------------------------------------------------------------------------

def test_financed_share_without_a_term_asks_for_it():
    with pytest.raises(pc.ComplexParseError, match=r"How long would you take to pay off the \$2,000 loan for the car"):
        parse("If I wanted to buy a used car for 4000, but financed 2000 of it, would that be fine?")


def test_financed_share_with_a_term_is_one_loan_not_two_purchases():
    d = pc.parse_complex("If I wanted to buy a used car for 4000, but financed 2000 of it over 2 years", TODAY,
                         use_llm=False, personal_loan_apr=0.12)
    down, payment = d.items
    assert (down.amount, down.label) == (2000, "Car down payment")
    assert payment.end_day == 24 * 30 and d.terms["loans"][0]["principal"] == 2000


def test_pronouns_are_never_labels():
    assert pc.clean_label("it", "Expense") == "Expense" and pc.clean_label("that", "Loan") == "Loan"
