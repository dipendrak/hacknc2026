"""Dev B's synchronous worker pipeline and frozen HTTP surface."""
from __future__ import annotations

import logging
import os
import time
import uuid
from contextvars import ContextVar
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from api.pipeline import (NO_CHANGE, Pipeline, RephraseRequired, check_evidence, checked_template,
                          claims_cache_key, fake_gemini_render, get_cached_render, parse,
                          readable_response, save_cached_render, test_fixture_customer,
                          test_fixture_delta, validate, validate_decision, validation_passed)
from api.audit import audit_decision, audit_response, parse_audit, priorities_for
from api.cost import habit_decision, habit_response, parse_habit
from api.settings import SettingsSource
from api.store import RunStore
from core.audit import audit as run_audit
from core.feasible import find_feasible_start
from core.financing import financing_options
from core.housing import resolve_housing
from core.income import resolve_income_changes
from data.cache import CacheRepository
from data.demo import demo_snapshots, merge_fixture_snapshots
from data.fit import FitError, fit

logger = logging.getLogger(__name__)

# The asking customer's settings, so the default parser (which only receives the text) can use their own
# tax share and loan rates. Set per request in ask(); ContextVars don't leak between concurrent requests.
_request_settings: ContextVar = ContextVar("request_settings", default=None)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=4000)
    customer_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


class AskResponse(BaseModel):
    run_id: str
    prose: str
    replay_url: str
    validated: bool
    rendered_by: str  # "gemini_rendered" | "cache_hit" | "template_fallback" | "test_fixture"
    render_failure_reason: str | None = None  # why it fell back, when it did
    mode: str = "decision"  # "habit" for how-much-would-I-lose-per-year questions, "audit" for what-can-I-cut ones


def create_app(*, cache_dir: Path | None = None, db_path: Path | None = None,
               replay_base_url: str | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    load_dotenv()
    cache_dir = cache_dir or Path(os.getenv("COUNTERFACTUAL_CACHE_DIR", "data/cache"))
    db_path = db_path or Path(os.getenv("COUNTERFACTUAL_DB_PATH", "data/runs.sqlite3"))
    replay_base_url = (replay_base_url or os.getenv("REPLAY_BASE_URL", "http://localhost:3000")).rstrip("/")
    if not replay_base_url.startswith(("https://", "http://")):
        raise ValueError("REPLAY_BASE_URL must be an absolute HTTP(S) frontend origin")
    mode = os.getenv("COUNTERFACTUAL_PARSER", "auto")
    if mode not in ("auto", "llm", "rules", "explicit"):
        raise ValueError("COUNTERFACTUAL_PARSER must be auto, llm, rules, or explicit")
    test_triggers_raw = os.getenv("COUNTERFACTUAL_TEST_TRIGGERS", "1")
    if test_triggers_raw not in ("0", "1"):
        raise ValueError("COUNTERFACTUAL_TEST_TRIGGERS must be 0 or 1")
    test_triggers_enabled = test_triggers_raw == "1"
    # Tax, interest rates and simulation size from Supabase's settings table (code defaults otherwise).
    settings = SettingsSource()
    pipeline = pipeline or Pipeline(parser=lambda text: parse(text, mode, settings=_request_settings.get() or settings()))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.cache = CacheRepository(cache_dir)
        if test_triggers_enabled:
            # Demo-safety fixtures for the test1..test5 trigger below. In-memory
            # only, additive-only (never overwrites a real cached customer_id).
            merge_fixture_snapshots(app.state.cache, demo_snapshots())
        # RUN_STORE=postgres keeps replay records in Supabase (DATABASE_URL) so a host with no persistent disk can serve them.
        use_pg = os.getenv("RUN_STORE", "sqlite") == "postgres"
        app.state.store = RunStore(db_path, os.getenv("DATABASE_URL") if use_pg else None)
        app.state.models = {}
        yield

    app = FastAPI(title="Counterfactual API", lifespan=lifespan)
    app.state.pipeline = pipeline
    app.state.settings = settings

    def model_for(customer_id: str):
        try:
            snapshot = app.state.cache.snapshot(customer_id)
        except KeyError:
            raise HTTPException(404, "Customer is not cached; ingest it before asking a question") from None
        try:
            if customer_id not in app.state.models:
                model = fit(snapshot)
                for tid in model.evidence_index:
                    app.state.cache.transaction(tid)
                app.state.models[customer_id] = model
            return app.state.models[customer_id]
        except (FitError, ValueError, KeyError) as exc:
            logger.warning("model_fit_failed customer_id=%s error_type=%s", customer_id, type(exc).__name__)
            raise HTTPException(422, "The cached history cannot support a cashflow model; check the ingest report") from None
        except Exception as exc:
            logger.error("model_load_failed error_type=%s", type(exc).__name__)
            raise HTTPException(503, "Cached model is unavailable; please try again later") from None

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.get("/customers")
    def customers():
        return app.state.cache.customers()

    @app.get("/model/{customer_id}")
    def model(customer_id: str):
        return model_for(customer_id).to_dict()

    @app.get("/txn/{txn_id}")
    def transaction(txn_id: str, response: Response):
        try:
            response.headers["X-Monetary-Unit"] = app.state.cache.transaction_unit(txn_id)
            return app.state.cache.transaction(txn_id)
        except KeyError:
            try:
                response.headers["X-Monetary-Unit"] = app.state.store.transaction_unit(txn_id)
                return app.state.store.transaction(txn_id)
            except KeyError:
                raise HTTPException(404, "Transaction is not cached") from None
            except Exception as exc:
                logger.error("evidence_load_failed error_type=%s", type(exc).__name__)
                raise HTTPException(503, "Archived evidence is temporarily unavailable") from None

    @app.get("/customers/{customer_id}/runs")
    def customer_runs(customer_id: str, limit: int = 30):
        """This customer's recent replays (question, verdict, time), from every channel."""
        if customer_id not in app.state.cache.snapshots:
            raise HTTPException(404, "Customer is not cached")
        try:
            return app.state.store.recent(customer_id, max(1, min(limit, 100)))
        except Exception as exc:
            logger.error("recent_runs_failed error_type=%s", type(exc).__name__)
            raise HTTPException(503, "Recent replays are temporarily unavailable") from None

    @app.get("/run/{run_id}")
    def stored_run(run_id: str):
        try:
            return app.state.store.get(run_id)
        except KeyError:
            raise HTTPException(404, "Run was not found") from None
        except Exception as exc:
            logger.error("replay_load_failed error_type=%s", type(exc).__name__)
            raise HTTPException(503, "Stored replay is temporarily unavailable") from None

    @app.get("/run/{run_id}/inputs")
    def run_inputs(run_id: str):
        # Additive route: D can show the actual model used rather than a later refit.
        try:
            return app.state.store.inputs(run_id)
        except KeyError:
            raise HTTPException(404, "Run was not found") from None
        except Exception as exc:
            logger.error("replay_inputs_failed error_type=%s", type(exc).__name__)
            raise HTTPException(503, "Stored replay inputs are temporarily unavailable") from None

    @app.post("/ask", response_model=AskResponse)
    def ask(body: AskRequest):
        started = time.perf_counter()
        # Demo-safety trigger: text is exactly test1..test5 -> use the mapped
        # synthetic demo_* customer instead of body.customer_id, and a fixed
        # known-good decision instead of parsing "test1" as a question.
        fixture_customer = test_fixture_customer(body.text) if test_triggers_enabled else None
        customer_id = fixture_customer or body.customer_id
        # Cheap customer check before any potentially paid parsing call.
        if customer_id not in app.state.cache.snapshots:
            raise HTTPException(404, "Customer is not cached")
        # This person's tax share and loan rates; simulation size and horizon are shared by everyone.
        s = settings(customer_id)
        _request_settings.set(s)
        try:
            audit_asked = False
            habit = None if fixture_customer is not None else parse_habit(body.text)
            if fixture_customer is not None:
                delta = validate_decision(test_fixture_delta(body.text))
            elif habit is not None:
                delta = validate_decision(habit_decision(habit, body.text))
            elif parse_audit(body.text):
                audit_asked, delta = True, None  # built below, once the customer's model is known
            else:
                delta = validate_decision(pipeline.parser(body.text))
        except RephraseRequired as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            logger.warning("parse_failed error_type=%s", type(exc).__name__)
            raise HTTPException(422, "Please rephrase with a specific amount and date; parsing was unavailable") from None
        model = model_for(customer_id)
        audit_result = None
        if audit_asked:
            audit_result = run_audit(model, priorities_for(model))
            if not audit_result.rows:
                raise HTTPException(422, "I don't see any low-priority recurring expenses to cut. "
                                         "Everything recurring looks essential.")
            delta = validate_decision(audit_decision(audit_result, body.text))
        special = habit is not None or audit_result is not None
        try:
            delta = resolve_housing(resolve_income_changes(delta, model), model)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            result = pipeline.simulator(model, delta, run_id=str(uuid.uuid4()), n_paths=s.simulation_paths,
                                        horizon_weeks=s.horizon_weeks, breach_threshold=s.breach_threshold)
            check_evidence(result, app.state.cache.transaction)
            fallback = checked_template(result)
        except Exception as exc:
            logger.error("simulation_failed error_type=%s", type(exc).__name__)
            raise HTTPException(503, "The simulation failed; no financial estimate was produced") from None
        prose = fallback
        render_failure_reason = None
        if special:
            rendered_by = "template_fallback"  # the reply is built from the numbers, no LLM render needed
        elif fixture_customer is not None:
            # Never touch Gemini or the shared render cache for a triggered
            # demo-safety request: zero network calls, fast, deterministic,
            # and distinguishable in logs/replay from a genuine render or
            # template fallback. Still runs through the real validator for
            # consistency, though fake_gemini_render is validator-safe by
            # construction, so this should never actually fall through.
            candidate = fake_gemini_render(result.claims, result.culprit)
            if validation_passed(validate(candidate, result.claims, result.culprit)):
                prose, rendered_by = candidate, "test_fixture"
            else:
                rendered_by = "template_fallback"
        else:
            rendered_by = "template_fallback"
            cache_key = claims_cache_key(result.claims)
            cached = get_cached_render(cache_key)
            if cached is not None:
                prose = cached
                rendered_by = "cache_hit"
            else:
                # One attempt only, no retry: a failed render falls straight
                # through to the already-computed template. Retrying is what
                # exhausted the free-tier quota previously.
                try:
                    candidate = pipeline.renderer(result.claims, result.culprit)
                    if not isinstance(candidate, str) or not candidate.strip():
                        raise ValueError("Renderer returned empty prose")
                    if validation_passed(pipeline.validator(candidate, result.claims, result.culprit)):
                        prose = candidate
                        rendered_by = "gemini_rendered"
                        save_cached_render(cache_key, candidate)
                    else:
                        render_failure_reason = "validation_rejected"
                except ModuleNotFoundError as exc:
                    logger.info("render_dependency_unavailable module=%s", exc.name)
                    render_failure_reason = f"module_unavailable:{exc.name}"
                except Exception as exc:
                    logger.warning("render_failed error_type=%s error=%s", type(exc).__name__, exc)
                    render_failure_reason = f"{type(exc).__name__}: {exc}"
        # Applies regardless of path (test_fixture, gemini_rendered, cache_hit,
        # template_fallback): wraps whatever prose ended up above with
        # decision/date/threshold context derived straight from the checked
        # simulation output, not from the LLM.
        outlook = financing = None
        # Only when this decision itself raises the odds: if they're already high without it,
        # waiting or financing it can't help.
        if (not special and result.delta_breach_rate is not None and result.delta_breach_rate > s.safe_breach_rate
                and result.delta_breach_rate > result.baseline_breach_rate + NO_CHANGE):
            try:
                outlook = find_feasible_start(model, delta, seed=result.seed, n_paths=result.n_paths,
                                              horizon_weeks=result.horizon_weeks, safe_rate=s.safe_breach_rate,
                                              breach_threshold=result.breach_threshold)
                financing = financing_options(model, delta, rates=s.rates, seed=result.seed, n_paths=result.n_paths,
                                              horizon_weeks=result.horizon_weeks, safe_rate=s.safe_breach_rate,
                                              breach_threshold=result.breach_threshold)
            except Exception as exc:  # the verdict stands without the suggestions
                logger.warning("outlook_failed error_type=%s", type(exc).__name__)
        if audit_result is not None:
            prose = audit_response(audit_result, result.horizon_weeks / 52, float(result.baseline_breach_rate),
                                   float(result.delta_breach_rate), result.breach_threshold)
        elif habit is not None:
            prose = habit_response(habit, model, float(result.delta_breach_rate), float(result.baseline_breach_rate),
                                   result.horizon_weeks / 52, result.breach_threshold)
        else:
            prose = readable_response(result, delta, prose, date.today(), outlook, financing)
        response = {"run_id": result.run_id, "prose": prose,
                    "replay_url": f"{replay_base_url}/r/{result.run_id}", "validated": True,
                    "rendered_by": rendered_by, "render_failure_reason": render_failure_reason,
                    "mode": "audit" if audit_result is not None else "habit" if habit is not None else "decision"}
        try:
            evidence = {tid: app.state.cache.transaction(tid) for tid in model.evidence_index}
            units = {tid: app.state.cache.transaction_unit(tid) for tid in evidence}
            app.state.store.save(result, model, delta, response, evidence, evidence_units=units)
        except Exception as exc:
            logger.error("run_persist_failed error_type=%s", type(exc).__name__)
            raise HTTPException(503, "The replay could not be saved; please retry your question") from None
        elapsed = time.perf_counter() - started
        logger.info("ask_completed run_id=%s elapsed_seconds=%.4f", result.run_id, elapsed)
        return response

    return app


app = create_app()
