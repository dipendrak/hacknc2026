from dataclasses import replace
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.pipeline import Pipeline, explicit_parse, fake_gemini_render, validate, validate_delta
from core.model import DecisionDelta
from data.cache import write_snapshot
from data.demo import demo_snapshots, TEST_TRIGGER_CUSTOMERS
from data.money import USD_CENTS, CENTS_ACCOUNT_PREFIX, to_cents


@pytest.fixture
def setup(tmp_path, monkeypatch):
    # Requests using actual HTTP transport are forbidden; TestClient is in-process.
    def forbidden(*args, **kwargs):
        raise AssertionError("Runtime attempted network access")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr("api.pipeline.RENDER_CACHE_PATH", tmp_path / "render_cache.json")
    # Off by default: most tests here control cache state precisely (including
    # deleting all cached customers and asserting an empty result), which the
    # always-on startup fixture-merge would otherwise repopulate. Tests for the
    # test1..test5 trigger itself re-enable it explicitly.
    monkeypatch.setenv("COUNTERFACTUAL_TEST_TRIGGERS", "0")
    cache_dir = tmp_path / "cache"
    for snapshot in demo_snapshots():
        write_snapshot(cache_dir, snapshot)
    return {"cache_dir": cache_dir, "db_path": tmp_path / "runs.sqlite3", "replay_base_url": "https://replay.test"}


def offline_pipeline(**kwargs):
    def missing(*args):
        raise ModuleNotFoundError("Dev C absent")
    return Pipeline(parser=explicit_parse, renderer=kwargs.get("renderer", missing),
                    validator=kwargs.get("validator", missing), **{k: v for k, v in kwargs.items() if k not in ("renderer", "validator")})


def fake_gemini_pipeline(**kwargs):
    """A pipeline whose renderer/validator are real (fake_gemini_render, the
    real validate), but make zero network calls - fast and deterministic,
    unlike actually exercising llm.render.render against a live key."""
    return Pipeline(parser=explicit_parse, renderer=kwargs.get("renderer", fake_gemini_render),
                    validator=kwargs.get("validator", validate),
                    **{k: v for k, v in kwargs.items() if k not in ("renderer", "validator")})


def ask(client, cid="demo_steady", text="Can I afford $1400 today?"):
    return client.post("/ask", json={"text": text, "customer_id": cid})


def test_full_pipeline_all_evidence_and_durable_replay(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        assert client.get("/health").json() == {"ok": True}
        assert len(client.get("/customers").json()) == 5
        model = client.get("/model/demo_steady").json()
        for tid in model["evidence_index"]:
            assert client.get(f"/txn/{tid}").json()["_id"] == tid
        response = ask(client)
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"run_id", "prose", "replay_url", "validated", "rendered_by", "render_failure_reason", "mode"}
        assert payload["rendered_by"] == "template_fallback"
        assert payload["validated"] is True
        assert payload["replay_url"] == f"https://replay.test/r/{payload['run_id']}"
        result = client.get(f"/run/{payload['run_id']}").json()
        assert result["n_paths"] == 500 and result["horizon_weeks"] == 104
        assert len(result["paths"]) == 500 and len(result["paths"][0]["balances"]) == 105
        for claim in result["claims"]:
            assert all(client.get(f"/txn/{tid}").status_code == 200 for tid in claim["evidence"])
        inputs = client.get(f"/run/{payload['run_id']}/inputs").json()
        assert inputs["model"] == model
        assert inputs["delta"]["amount"] == 1400
    # Persisted results survive process/app restart and do not depend on cached refits.
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        assert client.get(f"/run/{payload['run_id']}").json() == result


def test_evidence_survives_removal_from_current_cache(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        payload = ask(client).json()
        inputs = client.get(f"/run/{payload['run_id']}/inputs").json()
        tid = next(iter(inputs["evidence"]))
        raw = client.get(f"/txn/{tid}").json()
    for path in setup["cache_dir"].glob("*.json"):
        path.unlink()
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        assert client.get("/customers").json() == []
        assert client.get(f"/txn/{tid}").json() == raw
        assert client.get(f"/run/{payload['run_id']}/inputs").json()["evidence"][tid] == raw


def test_cents_evidence_and_units_survive_restart_and_cache_removal(setup):
    snapshot = demo_snapshots()[0]
    bundle = snapshot["accounts"][0]
    bundle["monetary_unit"] = USD_CENTS
    bundle["account"].update(nickname=CENTS_ACCOUNT_PREFIX + "test", balance=231040)
    for collection in ("deposits", "purchases"):
        for row in bundle[collection]:
            row["amount"] = to_cents(row["amount"])
    write_snapshot(setup["cache_dir"], snapshot)
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        model = client.get("/model/demo_steady").json()
        assert model["opening_balance"] == 2310.4
        answer = ask(client).json()
        inputs = client.get(f"/run/{answer['run_id']}/inputs").json()
        assert inputs["delta"]["amount"] == 1400
        assert inputs["amount_units"]["model"] == "usd"
        assert set(inputs["amount_units"]["evidence"].values()) == {USD_CENTS}
        row = bundle["purchases"][-1]
        tid = row["_id"]
        transaction = client.get(f"/txn/{tid}")
        assert transaction.json() == row
        assert transaction.headers["X-Monetary-Unit"] == USD_CENTS
    for path in setup["cache_dir"].glob("*.json"):
        path.unlink()
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        transaction = client.get(f"/txn/{tid}")
        assert transaction.json() == row and transaction.headers["X-Monetary-Unit"] == USD_CENTS
        assert client.get(f"/run/{answer['run_id']}/inputs").json() == inputs


def test_existing_database_without_unit_rows_defaults_to_dollars(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        answer = ask(client).json()
        inputs = client.get(f"/run/{answer['run_id']}/inputs").json()
        tid = next(iter(inputs["evidence"]))
        with client.app.state.store.connect() as connection:
            connection.execute("DELETE FROM run_units")
            connection.execute("DELETE FROM transaction_units")
    for path in setup["cache_dir"].glob("*.json"):
        path.unlink()
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        assert client.get(f"/txn/{tid}").headers["X-Monetary-Unit"] == "usd"
        assert client.get(f"/run/{answer['run_id']}/inputs").json()["amount_units"] == inputs["amount_units"]


def test_each_fixture_can_be_asked_without_network(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        for customer in client.get("/customers").json():
            assert ask(client, customer["customer_id"]).status_code == 200


def test_default_auto_parser_when_dev_c_absent(setup, monkeypatch):
    monkeypatch.setenv("COUNTERFACTUAL_PARSER", "auto")
    with TestClient(create_app(**setup)) as client:
        assert ask(client).status_code == 200
        assert ask(client, text="Can I afford a car?").status_code == 422


@pytest.mark.parametrize("text", ["Can I afford a car?", "Can I afford $1400 in December?",
                                   "Can I afford $1400 today if I lose my job?", "Can I afford $0 today?",
                                   "spend $1400 tomorrow", "spend $-5 today"])
def test_ambiguous_or_unsupported_offline_input_rephrases(setup, text):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        assert ask(client, text=text).status_code == 422


@pytest.mark.parametrize("text,amount,day,cadence", [
    ("Can I afford $1,400 today?", 1400, 0, None),
    ("Can I afford $65/month starting today?", 65, 0, 30),
    ("spend $25/week in 30 days", 25, 30, 7)])
def test_explicit_parser(text, amount, day, cadence):
    delta = validate_delta(explicit_parse(text))
    assert (delta.amount, delta.start_day, delta.cadence_days) == (amount, day, cadence)


def test_low_confidence_and_parse_outage_rephrase(setup):
    pipeline = offline_pipeline()
    pipeline.parser = lambda text: DecisionDelta("one_time", 1400, 0, confidence=.2)
    with TestClient(create_app(**setup, pipeline=pipeline)) as client:
        assert ask(client).status_code == 422
        def broken(text):
            raise RuntimeError("parser offline")
        pipeline.parser = broken
        assert ask(client).status_code == 422


def test_single_render_attempt_and_template_on_failure(setup):
    calls = []
    def renderer(claims, culprit):
        calls.append(claims)
        return "There is a fabricated 999% risk."
    pipeline = offline_pipeline(renderer=renderer, validator=lambda prose, claims, culprit: {"passed": False})
    with TestClient(create_app(**setup, pipeline=pipeline)) as client:
        payload = ask(client).json()
    assert len(calls) == 1
    assert "999%" not in payload["prose"] and payload["validated"] is True


def test_valid_render_cached_without_another_render(setup):
    calls = []
    def renderer(claims, culprit):
        calls.append(1)
        return "approved prose"
    pipeline = offline_pipeline(renderer=renderer, validator=lambda prose, claims, culprit: {"passed": prose == "approved prose"})
    with TestClient(create_app(**setup, pipeline=pipeline)) as client:
        first = ask(client).json()
        second = ask(client).json()
        assert first["prose"] == second["prose"]
        # No contributor in this fixture, so the driver sentence is left out.
        assert "approved prose" not in first["prose"]
        assert first["rendered_by"] == "gemini_rendered"
        assert second["rendered_by"] == "cache_hit"
        assert first["run_id"] != second["run_id"]
    assert len(calls) == 1


def test_real_renderer_supports_a_null_contributor(setup):
    from llm.render import render
    assert render([], None) == "Review the simulation and its evidence in the replay."


def test_readable_fallback_explains_decision_and_shortfall(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        payload = ask(client).json()
        result = client.get(f"/run/{payload['run_id']}").json()
    prose = payload["prose"]
    assert "$1,400 today" in prose
    assert f"With it: {result['delta_breach_rate']:.0%}" in prose
    assert f"Without it: {result['baseline_breach_rate']:.0%}" in prose
    assert "dips below $0 in the next 2 years" in prose
    assert "not a guarantee" in prose
    assert "breach rate" not in prose and "marginal contribution" not in prose
    assert "\n\n" in prose and payload["rendered_by"] == "template_fallback"


def test_cached_narrative_uses_current_payment_context(setup):
    from core.run import run
    calls = []
    def renderer(*args):
        calls.append(1)
        return "Approved comparison."
    # Force identical simulation claims to exercise reuse across decisions.
    def same_claims(model, delta, **kwargs):
        return run(model, DecisionDelta("one_time", 1400, 0), **kwargs)
    pipeline = offline_pipeline(renderer=renderer, simulator=same_claims,
                                validator=lambda *args: {"passed": True})
    with TestClient(create_app(**setup, pipeline=pipeline)) as client:
        first = ask(client).json()
        second = ask(client, text="spend $65/month in 30 days").json()
    later = date.today() + timedelta(days=30)
    assert len(calls) == 1 and second["rendered_by"] == "cache_hit"
    assert "$1,400 today" in first["prose"]
    assert "$65 a month" in second["prose"]
    assert f"{later.strftime('%b')} {later.day}" in second["prose"]
    assert "$1,400" not in second["prose"]


def test_no_contributor_can_validate_readable_comparison():
    from core.model import Claim
    from llm.validate import validate
    from api.pipeline import template
    claims = [Claim("base", "baseline_breach_rate", 0.12, "probability", []),
              Claim("with", "breach_rate_with_delta", 0.2, "probability", [])]
    assert validate(template(claims), claims, None).passed


def test_validator_outage_cannot_release_unchecked_prose(setup):
    def broken(*args):
        raise RuntimeError("validator offline")
    pipeline = offline_pipeline(renderer=lambda *args: "unchecked 999%", validator=broken)
    with TestClient(create_app(**setup, pipeline=pipeline)) as client:
        response = ask(client)
        assert response.status_code == 200 and "999%" not in response.json()["prose"]


@pytest.mark.parametrize("value", ["true", 1, {}, {"passed": "false"}])
def test_validator_requires_boolean_true(setup, value):
    pipeline = offline_pipeline(renderer=lambda *args: "unchecked", validator=lambda *args: value)
    with TestClient(create_app(**setup, pipeline=pipeline)) as client:
        assert ask(client).json()["prose"] != "unchecked"


def test_simulation_failure_surfaces_honestly(setup):
    def broken(*args, **kwargs):
        raise RuntimeError("simulation failed")
    with TestClient(create_app(**setup, pipeline=offline_pipeline(simulator=broken))) as client:
        response = ask(client)
        assert response.status_code == 503
        assert "no financial estimate" in response.json()["detail"]


def test_missing_claim_evidence_cannot_release_estimate(setup):
    from core.run import run
    def broken(model, delta, **kwargs):
        result = run(model, delta, **kwargs)
        return replace(result, claims=[replace(c, evidence=["unresolved"]) for c in result.claims])
    with TestClient(create_app(**setup, pipeline=offline_pipeline(simulator=broken))) as client:
        assert ask(client).status_code == 503


def test_storage_failure_does_not_return_broken_replay(setup, monkeypatch):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        def broken(*args):
            raise OSError("storage full")
        monkeypatch.setattr(client.app.state.store, "save", broken)
        response = ask(client)
        assert response.status_code == 503
        assert "saved" in response.json()["detail"]


def test_missing_resources_and_request_validation(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        for path in ("/model/missing", "/txn/missing", "/run/missing", "/run/missing/inputs"):
            assert client.get(path).status_code == 404
        assert ask(client, cid="missing").status_code == 404
        assert ask(client, text=" ").status_code == 422
        assert client.post("/ask", json={"text": "x", "customer_id": "../bad"}).status_code == 422


def test_unsupported_model_is_422(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        client.app.state.cache.snapshots["demo_steady"]["accounts"][0]["deposits"] = []
        assert ask(client).status_code == 422


def test_repeated_questions_get_distinct_immutable_run_ids(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        first, second = ask(client).json(), ask(client).json()
        assert first["run_id"] != second["run_id"]
        a = client.get(f"/run/{first['run_id']}").json()
        b = client.get(f"/run/{second['run_id']}").json()
        assert a["paths"] == b["paths"] and a["claims"] == b["claims"]


def test_fake_gemini_renderer_passes_validation_without_network(setup):
    with TestClient(create_app(**setup, pipeline=fake_gemini_pipeline())) as client:
        payload = ask(client).json()
        assert payload["rendered_by"] == "gemini_rendered"
        assert payload["render_failure_reason"] is None
        # Reads like a verdict, not the stiff checked_template sentence.
        assert "simulated futures" not in payload["prose"]


@pytest.mark.parametrize("trigger,expected_customer", list(TEST_TRIGGER_CUSTOMERS.items()))
def test_trigger_bypasses_render_and_overrides_customer(setup, monkeypatch, trigger, expected_customer):
    monkeypatch.setenv("COUNTERFACTUAL_TEST_TRIGGERS", "1")
    render_calls, validate_calls = [], []
    def renderer(claims, culprit):
        render_calls.append(1)
        return "should never be called"
    def validator(prose, claims, culprit):
        validate_calls.append(1)
        return {"passed": True}
    pipeline = Pipeline(parser=explicit_parse, renderer=renderer, validator=validator)
    with TestClient(create_app(**setup, pipeline=pipeline)) as client:
        # customer_id is deliberately not cached, to prove the trigger overrides it.
        response = client.post("/ask", json={"text": trigger, "customer_id": "nonexistent-but-pattern-valid"})
        assert response.status_code == 200
        payload = response.json()
        assert payload["rendered_by"] == "test_fixture"
        assert payload["render_failure_reason"] is None
        assert render_calls == [] and validate_calls == []
        inputs = client.get(f"/run/{payload['run_id']}/inputs").json()
        assert inputs["model"]["customer_id"] == expected_customer


def test_trigger_is_case_insensitive_and_whitespace_tolerant(setup, monkeypatch):
    monkeypatch.setenv("COUNTERFACTUAL_TEST_TRIGGERS", "1")
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        response = client.post("/ask", json={"text": " Test1 ", "customer_id": "nonexistent-but-pattern-valid"})
        assert response.status_code == 200
        assert response.json()["rendered_by"] == "test_fixture"


@pytest.mark.parametrize("text", ["Test 1", "test1.", "test1!", "test1?", "TEST 1."])
def test_trigger_survives_phone_autocorrect_noise(setup, monkeypatch, text):
    # Real phones reliably mangle a bare "test1" into one of these on send
    # (inserted space, trailing punctuation) - previously fell through to the
    # real parser and surfaced a confusing "parsing was unavailable" error.
    monkeypatch.setenv("COUNTERFACTUAL_TEST_TRIGGERS", "1")
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        response = client.post("/ask", json={"text": text, "customer_id": "nonexistent-but-pattern-valid"})
        assert response.status_code == 200
        assert response.json()["rendered_by"] == "test_fixture"


def test_non_matching_trigger_like_text_falls_through_to_normal_parsing(setup, monkeypatch):
    monkeypatch.setenv("COUNTERFACTUAL_TEST_TRIGGERS", "1")
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        # "test6" isn't a mapped trigger and isn't a parseable question either.
        assert ask(client, text="test6").status_code == 422


def test_trigger_disabled_via_env_falls_through_to_normal_parsing(setup, monkeypatch):
    monkeypatch.setenv("COUNTERFACTUAL_TEST_TRIGGERS", "0")
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        response = ask(client, text="test1")
        assert response.status_code == 422
        assert "rendered_by" not in response.json()


def test_replay_inputs_include_when_asked_and_the_sent_message(setup):
    # The replay page turns week numbers into dates ("the week of Oct 31") from created_at.
    from datetime import datetime
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        payload = ask(client).json()
        inputs = client.get(f"/run/{payload['run_id']}/inputs").json()
    assert inputs["prose"] == payload["prose"]
    assert datetime.fromisoformat(inputs["created_at"]).date() <= date.today() + timedelta(days=1)


def real_parser_pipeline():
    from api.pipeline import parse
    return Pipeline(parser=parse, renderer=fake_gemini_render, validator=validate)


def test_complex_question_end_to_end(setup):
    text = "I just got a job making $90k a year, can I afford a $350k house with 20% down at 7% over 30 years?"
    with TestClient(create_app(**setup, pipeline=real_parser_pipeline())) as client:
        response = ask(client, text=text)
        assert response.status_code == 200, response.text
        payload = response.json()
        prose = payload["prose"]
        # the whole decision is described, with code-computed numbers
        assert "$280,000 loan at 7% over 30 years" in prose and "about $1,863 a month" in prose
        assert "$70,000 down" in prose and "earning about" in prose
        assert "about 22% of that income goes to taxes" in prose
        inputs = client.get(f"/run/{payload['run_id']}/inputs").json()
        items = inputs["decision"]["items"]
        # the demo customer pays rent, which ends when the house is bought
        assert [i["direction"] for i in items] == ["inflow", "outflow", "outflow", "inflow"]
        assert items[3]["label"] == "Rent ends" and "rent stops once you buy" in prose
        assert items[0]["amount"] > 0 and items[2]["end_day"] == 360 * 30  # income resolved vs fitted paycheck
        assert inputs["delta"] == items[0]


def test_unaccountable_numbers_are_refused_with_a_hint(setup):
    with TestClient(create_app(**setup, pipeline=real_parser_pipeline())) as client:
        # A home loan's missing term defaults to 30 years, but a car loan's term is never guessed.
        response = ask(client, text="can I afford a $30k car at 7%")
        assert response.status_code == 422
        assert "How long would you take to pay off the loan for the car" in response.json()["detail"]


def test_old_single_delta_runs_still_load_in_inputs(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        run_id = ask(client).json()["run_id"]
        store = client.app.state.store
        import json
        with store.connect() as c:  # simulate a pre-Decision row: flat delta json
            flat = DecisionDelta(kind="one_time", amount=1400, start_day=0).to_dict()
            c.execute("UPDATE runs SET delta_json = ? WHERE run_id = ?", (json.dumps(flat), run_id))
        inputs = client.get(f"/run/{run_id}/inputs").json()
        assert inputs["delta"]["amount"] == 1400 and len(inputs["decision"]["items"]) == 1


def test_recent_runs_lists_one_customers_replays_newest_first(setup):
    with TestClient(create_app(**setup, pipeline=offline_pipeline())) as client:
        first = ask(client, text="Can I afford $1400 today?").json()
        second = ask(client, text="spend $65/month in 30 days").json()
        other = client.post("/ask", json={"text": "Can I afford $1400 today?", "customer_id": "demo_irregular"})
        assert other.status_code == 200, other.text
        runs = client.get("/customers/demo_steady/runs").json()
        assert [r["run_id"] for r in runs] == [second["run_id"], first["run_id"]]  # not demo_irregular's run
        assert runs[1]["question"] == "Can I afford $1400 today?"
        assert runs[1]["verdict"] == first["prose"].split("\n\n")[0]
        assert client.get("/customers/demo_steady/runs", params={"limit": 1}).json()[0]["run_id"] == second["run_id"]
        assert client.get("/customers/nobody/runs").status_code == 404
