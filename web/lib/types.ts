// Mirrors the shared contracts in core/model.py. The Python side owns the
// authoritative shapes; this is the web app's read-only view of them.

export type ClaimKind =
  | "baseline_breach_rate"
  | "breach_rate_with_delta"
  | "culprit_label"
  | "culprit_marginal_contribution"
  | "culprit_amount"
  | "median_min_balance"
  | "first_breach_week"
  | "paths_simulated";

// Common values are "probability" | "usd" | "week" | "count", but
// culprit_label's value is the merchant name itself, not a number, so it
// carries no unit — confirmed live: {"kind":"culprit_label","unit":null}.
export type ClaimUnit = string | null;

export interface Claim {
  claim_id: string;
  kind: ClaimKind;
  // Numeric for every kind except culprit_label, whose value is the label
  // string (matches core.model.Claim.value: float | str).
  value: number | string;
  unit: ClaimUnit;
  evidence: string[];
}

export interface Culprit {
  obligation_id: string;
  label: string;
  amount: number;
  breach_rate_without: number;
  marginal_contribution: number;
  evidence: string[];
  // Present on the live API (core.model.Driver).
  essential: boolean;
}

export interface SimPath {
  path_id: number;
  balances: number[];
  min_balance: number;
  first_breach_week: number | null;
}

// One week's p10/p50/p90 across the baseline (no-decision) path set - lets
// the chart overlay baseline vs. decision-scenario without a second full
// 500-path array. Always present (equal to the decision scenario when no
// delta was supplied).
export interface WeeklyBand {
  week: number;
  p10: number;
  p50: number;
  p90: number;
}

export interface SimResult {
  run_id: string;
  seed: number;
  n_paths: number;
  horizon_weeks: number;
  breach_threshold: number;
  baseline_breach_rate: number;
  delta_breach_rate: number | null;
  baseline_band: WeeklyBand[];
  median_min_balance: number;
  culprit: Culprit | null;
  ranked_drivers: Culprit[];
  paths: SimPath[];
  claims: Claim[];
}

export interface TxnRecord {
  [key: string]: unknown;
}

export interface Customer { customer_id: string; name: string }
export interface CashflowModel {
  customer_id: string; opening_balance: number; fit_window_days: number;
  income: { cadence_days: number; mean: number; std: number; next_due_day: number; evidence: string[] };
  recurring: { obligation_id: string; label: string; amount: number; cadence_days: number; essential: boolean; evidence: string[] }[];
  discretionary: { weekly_mean: number; weekly_std: number; by_category: Record<string, number>; evidence: string[] };
  evidence_index: string[];
}
export interface DecisionDelta {
  kind: string; amount: number; start_day: number; cadence_days: number | null; raw_text: string; label: string;
  // Older runs predate these: outflow / no end.
  direction?: "outflow" | "inflow"; end_day?: number | null;
}
export interface LoanTerms {
  principal: number; rate_pct: number | null; years: number; monthly_payment: number; down_payment: number;
  start_day?: number; item_indexes: number[]; label?: string;
}
/** A question's full set of cash-flow items (house = down payment + mortgage + new job...). */
export interface Decision {
  items: DecisionDelta[]; label: string; raw_text: string; confidence: number;
  terms: { loans?: LoanTerms[]; assumptions?: string[]; net_factor?: number; audit?: { combined_yearly: number; rows: { label: string; yearly_savings: number }[] }; rent_ended?: { label: string; amount: number; cadence_days: number };
    assumed_loans?: { label: string; rate_pct?: number; years?: number; kind?: string }[] };
}
export interface RunInputs {
  model: CashflowModel; delta: DecisionDelta; decision?: Decision; evidence: Record<string, TxnRecord>;
  amount_units: { model: string; delta: string; evidence: Record<string, string> };
  // When the question was asked (UTC ISO) and the message sent back; absent on older APIs.
  created_at?: string; prose?: string | null;
}
export interface AskResponse {
  run_id: string; prose: string; replay_url: string; validated: boolean;
  rendered_by?: string; render_failure_reason?: string | null;
}
// customer_id is absent on entries saved before per-user history.
/** One row of GET /customers/{id}/runs: a replay asked from any channel (web, iMessage). */
export interface RecentRun { run_id: string; created_at: string; question: string; verdict: string; mode: string }
export interface SavedReplay { run_id: string; question: string; customer: string; prose: string; createdAt: string; customer_id?: string }
