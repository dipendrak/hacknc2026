"""Shared data contracts between the parser, simulation, API and web app.

Field renames go through the group chat. Always.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CLAIM_KINDS: tuple[str, ...] = (
    "baseline_breach_rate",
    "breach_rate_with_delta",
    "culprit_label",
    "culprit_marginal_contribution",
    "culprit_amount",
    "median_min_balance",
    "first_breach_week",
    "paths_simulated",
)

CLAIM_UNITS: tuple[str, ...] = ("probability", "usd", "week", "count")

DELTA_KINDS: tuple[str, ...] = ("one_time", "recurring")
DELTA_DIRECTIONS: tuple[str, ...] = ("outflow", "inflow")


def _require_positive_cadence(cadence: float, where: str) -> float:
    cadence = float(cadence)
    if not cadence > 0:
        raise ValueError(f"{where}: cadence_days must be > 0, got {cadence}")
    return cadence


# --------------------------------------------------------------------------- #
# 1. CashflowModel — B produces, A consumes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Income:
    cadence_days: float
    mean: float
    std: float
    next_due_day: float
    evidence: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Income:
        return cls(
            cadence_days=_require_positive_cadence(d["cadence_days"], "income"),
            mean=float(d["mean"]),
            std=float(d.get("std", 0.0)),
            next_due_day=float(d.get("next_due_day", 0.0)),
            evidence=list(d.get("evidence", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "cadence_days": self.cadence_days,
            "mean": self.mean,
            "std": self.std,
            "next_due_day": self.next_due_day,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class RecurringObligation:
    obligation_id: str
    label: str
    merchant_id: str | None
    amount: float
    cadence_days: float
    next_due_day: float
    evidence: list[str] = field(default_factory=list)
    # Can't realistically be cancelled or shopped around (housing, loan payments).
    # Set by Dev B. Essentials are ranked but only named culprit as a last resort.
    essential: bool = False
    # Variable spending (food delivery, fuel): per-charge amount spread, and the share of scheduled
    # occurrences that don't happen. Both default to 0 = fixed bill, which reproduces older results.
    amount_std: float = 0.0
    skip_prob: float = 0.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RecurringObligation:
        oid = str(d["obligation_id"])
        return cls(
            obligation_id=oid,
            label=str(d["label"]),
            merchant_id=d.get("merchant_id"),
            amount=float(d["amount"]),
            cadence_days=_require_positive_cadence(d["cadence_days"], f"recurring[{oid}]"),
            next_due_day=float(d.get("next_due_day", 0.0)),
            evidence=list(d.get("evidence", [])),
            essential=bool(d.get("essential", False)),
            amount_std=max(float(d.get("amount_std", 0.0)), 0.0),
            skip_prob=min(max(float(d.get("skip_prob", 0.0)), 0.0), 0.95),
        )

    def to_dict(self) -> dict[str, Any]:
        extra = {k: v for k, v in (("amount_std", self.amount_std), ("skip_prob", self.skip_prob)) if v}
        return {
            **extra,
            "obligation_id": self.obligation_id,
            "label": self.label,
            "merchant_id": self.merchant_id,
            "amount": self.amount,
            "cadence_days": self.cadence_days,
            "next_due_day": self.next_due_day,
            "evidence": list(self.evidence),
            "essential": self.essential,
        }


@dataclass(frozen=True)
class Discretionary:
    weekly_mean: float
    weekly_std: float
    by_category: dict[str, float] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Discretionary:
        return cls(
            weekly_mean=float(d["weekly_mean"]),
            weekly_std=float(d.get("weekly_std", 0.0)),
            by_category={str(k): float(v) for k, v in d.get("by_category", {}).items()},
            evidence=list(d.get("evidence", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "weekly_mean": self.weekly_mean,
            "weekly_std": self.weekly_std,
            "by_category": dict(self.by_category),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class CashflowModel:
    customer_id: str
    opening_balance: float
    fit_window_days: int
    income: Income
    recurring: list[RecurringObligation]
    discretionary: Discretionary
    evidence_index: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CashflowModel:
        recurring = [RecurringObligation.from_dict(r) for r in d.get("recurring", [])]
        ids = [r.obligation_id for r in recurring]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate obligation_id in recurring: {ids}")
        return cls(
            customer_id=str(d["customer_id"]),
            opening_balance=float(d["opening_balance"]),
            fit_window_days=int(d.get("fit_window_days", 0)),
            income=Income.from_dict(d["income"]),
            recurring=recurring,
            discretionary=Discretionary.from_dict(d["discretionary"]),
            evidence_index=list(d.get("evidence_index", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "opening_balance": self.opening_balance,
            "fit_window_days": self.fit_window_days,
            "income": self.income.to_dict(),
            "recurring": [r.to_dict() for r in self.recurring],
            "discretionary": self.discretionary.to_dict(),
            "evidence_index": list(self.evidence_index),
        }

    def all_evidence(self) -> list[str]:
        """evidence_index if B filled it, else the union of every element's evidence."""
        if self.evidence_index:
            return list(self.evidence_index)
        seen: dict[str, None] = {}
        for ev in (
            self.income.evidence,
            *(r.evidence for r in self.recurring),
            self.discretionary.evidence,
        ):
            for t in ev:
                seen.setdefault(t, None)
        return list(seen)


# --------------------------------------------------------------------------- #
# 2. DecisionDelta — C produces, A consumes
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class DecisionDelta:
    """One cash-flow item. Outflows cost money; inflows (raises, new income) add it."""

    kind: str
    amount: float
    start_day: float
    cadence_days: float | None = None
    label: str = ""
    raw_text: str = ""
    confidence: float = 1.0
    direction: str = "outflow"
    end_day: float | None = None  # recurring stops at this day offset (exclusive)

    def __post_init__(self) -> None:
        if self.kind not in DELTA_KINDS:
            raise ValueError(f"DecisionDelta.kind must be one of {DELTA_KINDS}, got {self.kind!r}")
        if self.direction not in DELTA_DIRECTIONS:
            raise ValueError(f"DecisionDelta.direction must be one of {DELTA_DIRECTIONS}, got {self.direction!r}")
        if self.kind == "recurring":
            if self.cadence_days is None:
                raise ValueError("DecisionDelta.cadence_days is required when kind='recurring'")
            _require_positive_cadence(self.cadence_days, "delta")
        if self.end_day is not None:
            if self.kind != "recurring":
                raise ValueError("DecisionDelta.end_day only applies to recurring items")
            if not self.end_day > self.start_day:
                raise ValueError("DecisionDelta.end_day must be after start_day")

    @property
    def signed_amount(self) -> float:
        """Positive = money out, negative = money in."""
        return self.amount if self.direction == "outflow" else -self.amount

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DecisionDelta:
        cadence = d.get("cadence_days")
        end = d.get("end_day")
        return cls(
            kind=str(d["kind"]),
            amount=float(d["amount"]),
            start_day=float(d.get("start_day", 0.0)),
            cadence_days=None if cadence is None else float(cadence),
            label=str(d.get("label", "")),
            raw_text=str(d.get("raw_text", "")),
            confidence=float(d.get("confidence", 1.0)),
            direction=str(d.get("direction", "outflow")),
            end_day=None if end is None else float(end),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "amount": self.amount,
            "start_day": self.start_day,
            "cadence_days": self.cadence_days,
            "label": self.label,
            "raw_text": self.raw_text,
            "confidence": self.confidence,
            "direction": self.direction,
            "end_day": self.end_day,
        }


@dataclass(frozen=True)
class Decision:
    """A question's full set of cash-flow items (house = down payment + mortgage + new job...)."""

    items: tuple[DecisionDelta, ...]
    label: str = ""
    raw_text: str = ""
    confidence: float = 1.0
    terms: dict[str, Any] = field(default_factory=dict)  # loan terms for wording, e.g. principal/rate/years/payment

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if not self.items:
            raise ValueError("Decision needs at least one item")

    @classmethod
    def single(cls, delta: DecisionDelta) -> Decision:
        return cls(items=(delta,), label=delta.label, raw_text=delta.raw_text, confidence=delta.confidence)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Decision:
        if "items" not in d:  # legacy flat delta
            return cls.single(DecisionDelta.from_dict(d))
        return cls(
            items=tuple(DecisionDelta.from_dict(i) for i in d["items"]),
            label=str(d.get("label", "")),
            raw_text=str(d.get("raw_text", "")),
            confidence=float(d.get("confidence", 1.0)),
            terms=dict(d.get("terms", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [i.to_dict() for i in self.items],
            "label": self.label,
            "raw_text": self.raw_text,
            "confidence": self.confidence,
            "terms": self.terms,
        }


def as_decision(d: DecisionDelta | Decision | None) -> Decision | None:
    if d is None or isinstance(d, Decision):
        return d
    return Decision.single(d)


# --------------------------------------------------------------------------- #
# 3. Claim — A produces, C renders and validates
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Claim:
    claim_id: str
    kind: str
    # Numeric for every kind except culprit_label, whose value is the label string.
    value: float | str
    # None only for culprit_label (a name has no unit). Raised with Dev C for Round 2.
    unit: str | None
    evidence: list[str]

    def __post_init__(self) -> None:
        if self.kind not in CLAIM_KINDS:
            raise ValueError(f"Claim.kind {self.kind!r} not in frozen vocabulary")
        if self.unit is not None and self.unit not in CLAIM_UNITS:
            raise ValueError(f"Claim.unit {self.unit!r} not in {CLAIM_UNITS}")

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Claim:
        return cls(
            claim_id=str(d["claim_id"]),
            kind=str(d["kind"]),
            value=d["value"],
            unit=d.get("unit"),
            evidence=list(d.get("evidence", [])),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "kind": self.kind,
            "value": self.value,
            "unit": self.unit,
            "evidence": list(self.evidence),
        }


# --------------------------------------------------------------------------- #
# 4. SimResult — A produces, B serves, C and D consume
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Driver:
    """One recurring obligation's leave-one-out effect. The culprit is the top Driver."""

    obligation_id: str
    label: str
    amount: float
    breach_rate_without: float
    marginal_contribution: float
    evidence: list[str] = field(default_factory=list)
    essential: bool = False

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Driver:
        return cls(
            obligation_id=str(d["obligation_id"]),
            label=str(d["label"]),
            amount=float(d["amount"]),
            breach_rate_without=float(d["breach_rate_without"]),
            marginal_contribution=float(d["marginal_contribution"]),
            evidence=list(d.get("evidence", [])),
            essential=bool(d.get("essential", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "label": self.label,
            "amount": self.amount,
            "breach_rate_without": self.breach_rate_without,
            "marginal_contribution": self.marginal_contribution,
            "evidence": list(self.evidence),
            "essential": self.essential,
        }


Culprit = Driver


@dataclass(frozen=True)
class WeeklyBand:
    """One week's p10/p50/p90 balance across the baseline (no-decision)
    path set — lets a consumer overlay baseline vs. decision-scenario
    without shipping a second full 500-path array."""

    week: int
    p10: float
    p50: float
    p90: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> WeeklyBand:
        return cls(week=int(d["week"]), p10=float(d["p10"]), p50=float(d["p50"]), p90=float(d["p90"]))

    def to_dict(self) -> dict[str, Any]:
        return {"week": self.week, "p10": self.p10, "p50": self.p50, "p90": self.p90}


@dataclass(frozen=True)
class PathRecord:
    path_id: int
    # balances[0] is the opening balance; balances[w] is the balance at the end of week w.
    balances: list[float]
    min_balance: float
    first_breach_week: int | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PathRecord:
        fbw = d.get("first_breach_week")
        return cls(
            path_id=int(d["path_id"]),
            balances=[float(b) for b in d["balances"]],
            min_balance=float(d["min_balance"]),
            first_breach_week=None if fbw is None else int(fbw),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_id": self.path_id,
            "balances": list(self.balances),
            "min_balance": self.min_balance,
            "first_breach_week": self.first_breach_week,
        }


@dataclass(frozen=True)
class SimResult:
    run_id: str
    seed: int
    n_paths: int
    horizon_weeks: int
    breach_threshold: float
    baseline_breach_rate: float
    # None when no DecisionDelta was supplied.
    delta_breach_rate: float | None
    median_min_balance: float
    culprit: Culprit | None
    ranked_drivers: list[Driver]
    paths: list[PathRecord]
    claims: list[Claim]
    # Always populated (equal to the decision band when no delta was given -
    # harmless, keeps the field non-optional for every consumer). Lets a
    # chart overlay "with this decision" against "baseline" without a second
    # full path array.
    baseline_band: list[WeeklyBand] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SimResult:
        dbr = d.get("delta_breach_rate")
        culprit = d.get("culprit")
        return cls(
            run_id=str(d["run_id"]),
            seed=int(d["seed"]),
            n_paths=int(d["n_paths"]),
            horizon_weeks=int(d["horizon_weeks"]),
            breach_threshold=float(d["breach_threshold"]),
            baseline_breach_rate=float(d["baseline_breach_rate"]),
            delta_breach_rate=None if dbr is None else float(dbr),
            median_min_balance=float(d["median_min_balance"]),
            culprit=None if culprit is None else Driver.from_dict(culprit),
            ranked_drivers=[Driver.from_dict(x) for x in d.get("ranked_drivers", [])],
            paths=[PathRecord.from_dict(p) for p in d.get("paths", [])],
            claims=[Claim.from_dict(c) for c in d.get("claims", [])],
            baseline_band=[WeeklyBand.from_dict(b) for b in d.get("baseline_band", [])],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seed": self.seed,
            "n_paths": self.n_paths,
            "horizon_weeks": self.horizon_weeks,
            "breach_threshold": self.breach_threshold,
            "baseline_breach_rate": self.baseline_breach_rate,
            "delta_breach_rate": self.delta_breach_rate,
            "median_min_balance": self.median_min_balance,
            "culprit": None if self.culprit is None else self.culprit.to_dict(),
            "ranked_drivers": [x.to_dict() for x in self.ranked_drivers],
            "paths": [p.to_dict() for p in self.paths],
            "claims": [c.to_dict() for c in self.claims],
            "baseline_band": [b.to_dict() for b in self.baseline_band],
        }
