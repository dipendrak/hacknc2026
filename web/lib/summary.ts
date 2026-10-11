// Plain-English summary of a run, shared by the replay page and the image
// cards. Mirrors readable_response / dip_summary in api/pipeline.py so the
// text message, replay and cards say the same thing - keep them in sync.
import type { Culprit, Decision, DecisionDelta, LoanTerms, SimResult } from "./types";

// A recurring charge must move the odds by at least this much to be called out.
export const CULPRIT_MIN_CONTRIBUTION = 0.05;

export const money = (n: number) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: Number.isInteger(n) ? 0 : 2 }).format(n);
export const money0 = (n: number) =>
  new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }).format(n);

export function odds(rate: number): string {
  if (rate > 0 && rate < 0.01) return "under 1%";
  if (rate > 0.99 && rate < 1) return "over 99%";
  return `${Math.round(rate * 100)}%`;
}

/** "Jordan Synthetic Seed e3ed4c8012ad" -> "Jordan" */
export const displayName = (name: string) => name.replace(/\s+Synthetic Seed\b.*$/i, "").trim();

export function horizonLabel(weeks: number): string {
  if (weeks % 52 === 0) return weeks === 52 ? "year" : `${weeks / 52} years`;
  return `${weeks} weeks`;
}

export function weekLabel(week: number): string {
  if (week === 0) return "Today";
  if (week % 52 === 0) return week === 52 ? "1 year" : `${week / 52} years`;
  return `Week ${week}`;
}

const addDays = (d: Date, days: number) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + days);
const shortDate = (d: Date) => d.toLocaleDateString("en-US", { month: "short", day: "numeric" });

export function weekDate(asked: Date | null, week: number): string | null {
  return asked ? shortDate(addDays(asked, week * 7)) : null;
}

export function culpritMatters(culprit: Culprit | null): culprit is Culprit {
  return culprit !== null && culprit.marginal_contribution >= CULPRIT_MIN_CONTRIBUTION;
}

export interface Dip { share: number; firstWeek: number; weeksBelow: number; recovers: boolean }

const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)];

/** When, how deep and how long the typical shortfall is, across paths that have one. */
export function dipSummary(result: SimResult): Dip | null {
  const dipped = result.paths.filter(p => p.first_breach_week !== null);
  if (!dipped.length) return null;
  return {
    share: dipped.length / result.paths.length,
    firstWeek: median(dipped.map(p => p.first_breach_week as number)),
    weeksBelow: median(dipped.map(p => p.balances.slice(1).filter(b => b < result.breach_threshold).length)),
    recovers: median(result.paths.map(p => p.balances[p.balances.length - 1])) > result.breach_threshold,
  };
}

const whenPhrase = (startDay: number, asked: Date | null): string => {
  const day = asked ? addDays(asked, startDay) : null;
  return startDay === 0 ? "today" : startDay === 1 && asked ? "tomorrow"
    : day ? `on ${shortDate(day)}${day.getFullYear() !== asked!.getFullYear() ? `, ${day.getFullYear()}` : ""}`
    : `in ${startDay} days`;
};
const everyPhrase = (days: number) =>
  ({ 1: "a day", 7: "a week", 14: "every two weeks", 30: "a month", 365: "a year" } as Record<number, string>)[days] ?? `every ${days} days`;
const termPhrase = (years: number) => {
  const months = Math.round(years * 12);
  if (months % 12 === 0) return months === 12 ? "1 year" : `${months / 12} years`;
  return months === 1 ? "1 month" : `${months} months`;
};
const round0 = (n: number) => Math.floor(n + 0.5);

// Labels that name nothing ("Expense") or are internal; the wording leaves them out. Mirrors api/pipeline.py.
const GENERIC_LABELS = new Set(["expense", "purchase", "explicit expense", "test fixture expense", "habit spending", "income",
  "one-time income", "extra income", "new income", "pay change", "rent ends", "loan", "afford"]);
const ONE_TIME_INCOME = new Set(["bonus", "gift", "refund", "prize", "tax refund", "payout"]);
const PRONOUNS = new Set(["it", "this", "that", "them", "these", "those", "one", "something", "stuff", "thing"]);
// Odds within one point of the baseline count as "no change" (simulation noise).
export const NO_CHANGE = 0.01;

/** A label as it reads mid-sentence ("iPhone 18 Pro", "house"), or null if it names nothing. */
export function noun(label: string | undefined | null, rawText = ""): string | null {
  const l = (label ?? "").trim();
  if (!l || GENERIC_LABELS.has(l.toLowerCase()) || PRONOUNS.has(l.toLowerCase()) || l.length > 30 || l.split(/\s+/).length > 4
    || /[$?/]/.test(l) || / (payment|proceeds)$/i.test(l)) return null;
  const at = rawText.toLowerCase().indexOf(l.toLowerCase());
  if (at >= 0) return rawText.slice(at, at + l.length);
  return l.length > 1 && l[1] === l[1].toUpperCase() && l[1] !== l[1].toLowerCase() ? l : l.charAt(0).toLowerCase() + l.slice(1);
}

/** "A and B", or "A, B, and C"; a comma also separates two parts that each contain "and". */
function join(phrases: string[]): string {
  if (phrases.length <= 1) return phrases.join("");
  if (phrases.length === 2 && !phrases.some(p => p.includes(" and "))) return phrases.join(" and ");
  return `${phrases.slice(0, -1).join(", ")}, and ${phrases[phrases.length - 1]}`;
}

function itemPhrase(item: DecisionDelta, asked: Date | null, rawText = ""): string {
  const when = whenPhrase(item.start_day, asked);
  const thing = noun(item.label, rawText);
  if (item.kind === "recurring" && item.cadence_days) {
    // "for Netflix" (a brand) but "for the gym membership"
    const forWhat = thing ? (thing[0] === thing[0].toUpperCase() && thing[0] !== thing[0].toLowerCase() ? ` for ${thing}` : ` for the ${thing}`) : "";
    let phrase = `adding ${money(item.amount)} ${everyPhrase(item.cadence_days)}${forWhat} starting ${when.replace(/^on /, "")}`;
    if (item.end_day != null) {
      const months = Math.round((item.end_day - item.start_day) / 30);
      phrase += ` for ${months} month${months !== 1 ? "s" : ""}`;
    }
    return phrase;
  }
  return `spending ${money(item.amount)}${thing ? ` on the ${thing}` : ""} ${when}`;
}

/** Same-day purchases as one: "spending $639 today on the PS5 ($499) and the games ($140)". */
function purchasesPhrase(items: DecisionDelta[], asked: Date | null, rawText: string): string {
  const when = whenPhrase(items[0].start_day, asked);
  const total = money(items.reduce((sum, it) => sum + it.amount, 0));
  const things = items.map(it => noun(it.label, rawText));
  if (!things.every(Boolean)) return `spending ${total} ${when}`;
  return `spending ${total} ${when} on ${join(items.map((it, i) => `the ${things[i]} (${money(it.amount)})`))}`;
}

function incomePhrase(item: DecisionDelta, asked: Date | null): string {
  if (item.kind === "one_time") {  // a bonus, gift or refund
    const when = whenPhrase(item.start_day, asked), kind = item.label.toLowerCase();
    return ONE_TIME_INCOME.has(kind) ? `getting a ${money(item.amount)} ${kind} ${when}` : `getting ${money(item.amount)} ${when}`;
  }
  const perMonth = item.amount * (365 / 12) / (item.cadence_days as number);
  return `earning about ${money(round0(perMonth))} ${item.direction === "inflow" ? "more" : "less"} a month`;
}

function loanPhrase(loan: LoanTerms, asked: Date | null, rawText: string): string {
  const when = whenPhrase(loan.start_day ?? 0, asked);
  const terms = loan.rate_pct == null
    ? `paying ${money(loan.monthly_payment)} a month for ${termPhrase(loan.years)}`
    : `at ${loan.rate_pct}% over ${termPhrase(loan.years)} (about ${money(round0(loan.monthly_payment))} a month)`;
  const thing = noun(loan.label, rawText), down = loan.down_payment;
  const text = thing
    ? `buying the ${thing} with ${down > 0 ? `${money(down)} down and ` : ""}a ${money(loan.principal)} loan ${terms}`
    : down > 0 ? `borrowing ${money(loan.principal)} ${terms} with ${money(down)} down`
    : `borrowing ${money(loan.principal)} ${terms}`;
  return when === "today" ? text : `${text} ${when}`;
}

/** Mirrors describe_decision in api/pipeline.py: every number comes from parsed inputs or code math. */
export function describeDecision(decision: Decision, asked: Date | null): string {
  const audit = decision.terms.audit;
  if (audit) return `Stopping your ${audit.rows.length} lowest-priority recurring expense${audit.rows.length === 1 ? "" : "s"}`;
  const raw = decision.raw_text || decision.items[0]?.raw_text || "";
  const loans = decision.terms.loans ?? [];
  const loanIdx = new Set(loans.flatMap(l => l.item_indexes));
  const shown = decision.items.filter((it, idx) => !loanIdx.has(idx) && it.amount !== 0 && it.label !== "Rent ends");
  const spends = shown.filter(it => it.kind === "one_time" && (it.direction ?? "outflow") === "outflow");
  const together = spends.length > 1 && new Set(spends.map(it => it.start_day)).size === 1;
  const phrases: string[] = [];
  for (const item of shown) {
    if (item.direction === "inflow" || item.label === "Pay change") phrases.push(incomePhrase(item, asked));
    else if (together && spends.includes(item)) { if (item === spends[0]) phrases.push(purchasesPhrase(spends, asked, raw)); }
    else phrases.push(itemPhrase(item, asked, raw));
  }
  phrases.push(...loans.map(loan => loanPhrase(loan, asked, raw)));
  const text = join(phrases) || "making this change";
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** True when the decision brings money in (a raise, a bonus), not just a cost or a loan's cash. */
function addsIncome(delta: DecisionDelta | Decision | null): boolean {
  if (!delta) return false;
  if (!("items" in delta)) return delta.direction === "inflow";
  const loanIdx = new Set((delta.terms.loans ?? []).flatMap(l => l.item_indexes));
  return delta.items.some((it, idx) => it.direction === "inflow" && !loanIdx.has(idx) && it.label !== "Rent ends");
}

/** Disclosed assumptions behind the figures, or null. Mirrors decision_notes in api/pipeline.py. */
export function decisionNote(decision: Decision | null): string | null {
  const t = decision?.terms;
  const notes: string[] = [];
  for (const loan of t?.assumed_loans ?? []) {
    const parts: string[] = [];
    if (loan.rate_pct != null) parts.push(`your typical ${loan.rate_pct}% ${loan.kind} rate`);
    if (loan.years != null) parts.push(`a ${loan.years}-year term`);
    notes.push(`You didn't give a ${loan.rate_pct != null ? "rate" : "term"}${parts.length === 2 ? " or term" : ""} `
      + `for the ${loan.label.toLowerCase()}, so this assumes ${parts.join(" and ")}.`);
  }
  if (t?.assumptions?.includes("income_tax_assumed") && t.net_factor != null)
    notes.push(`Assumes about ${Math.round((1 - t.net_factor) * 100)}% of that income goes to taxes.`);
  if (t?.assumptions?.includes("rent_ends") && t.rent_ended)
    notes.push(`Assumes your ${money(t.rent_ended.amount)} rent stops once you buy. `
      + "Taxes, insurance and upkeep on the home are not included.");
  return notes.length ? notes.join(" ") : null;
}

function decisionPhrase(delta: DecisionDelta | Decision | null, asked: Date | null): string {
  if (!delta) return "This decision";
  if ("items" in delta) return describeDecision(delta, asked);
  const day = asked ? addDays(asked, delta.start_day) : null;
  const when = delta.start_day === 0 ? "today" : delta.start_day === 1 && asked ? "tomorrow"
    : day ? `on ${shortDate(day)}${day.getFullYear() !== asked!.getFullYear() ? `, ${day.getFullYear()}` : ""}`
    : `in ${delta.start_day} days`;
  if (delta.kind === "recurring" && delta.cadence_days) {
    const every = ({ 1: "a day", 7: "a week", 14: "every two weeks", 30: "a month", 365: "a year" } as Record<number, string>)[delta.cadence_days]
      ?? `every ${delta.cadence_days} days`;
    return `Adding ${money(delta.amount)} ${every} starting ${when.replace(/^on /, "")}`;
  }
  return `Spending ${money(delta.amount)} ${when}`;
}

/** One-sentence verdict, same tiers as lib/verdict.ts. Mirrors readable_response's lead in api/pipeline.py. */
export function verdictSentence(result: SimResult, delta: DecisionDelta | Decision | null, asked: Date | null): string {
  const rate = result.delta_breach_rate ?? result.baseline_breach_rate, base = result.baseline_breach_rate;
  const what = decisionPhrase(delta, asked);
  if (rate === 0) return `${what} looks safe. Your balance stayed above ${money0(result.breach_threshold)} in every simulation.`;
  if (result.delta_breach_rate !== null && addsIncome(delta) && rate < base - NO_CHANGE)
    return `${what} would lower your odds of running short.${rate >= 0.5 ? " Your cash is still tight either way." : ""}`;
  if (result.delta_breach_rate === null || rate <= base + NO_CHANGE)
    return base >= 0.5 ? "Your cash is already tight, with or without this." : `${what} doesn't really change your odds.`;
  if (rate < 0.2) return `${what} looks manageable.`;
  if (rate < 0.5) return `${what} is doable, but worth a second look.`;
  return `Heads up: ${what.charAt(0).toLowerCase()}${what.slice(1)} would be a real stretch.`;
}

const weeksOrMonths = (weeks: number) => weeks <= 1 ? "about a week" : weeks < 9 ? `about ${weeks} weeks`
  : `about ${Math.max(2, Math.floor(weeks / (52 / 12) + 0.5))} months`;

/** When/how deep the shortfall is, or null when there's nothing useful to say. */
export function dipSentence(result: SimResult, asked: Date | null): string | null {
  const dip = dipSummary(result);
  const rate = result.delta_breach_rate ?? result.baseline_breach_rate;
  if (!dip || rate <= result.baseline_breach_rate && result.delta_breach_rate !== null) return null;
  const date = weekDate(asked, dip.firstWeek);
  const when = date ? `the week of ${date}` : `about ${dip.firstWeek} week${dip.firstWeek === 1 ? "" : "s"} in`;
  if (dip.share < 0.5) return `If it does go wrong, the tight spot is most likely ${when}.`;
  // Most paths dip here, so the all-path median low is the typical one and
  // matches the "Typical lowest balance" stat next to this sentence.
  const short = `${money0(Math.round(Math.abs(result.median_min_balance - result.breach_threshold)))} short`;
  const below = `Most likely you'd drop below ${money0(result.breach_threshold)} ${when}`;
  if (!dip.recovers) return `${below} and not climb back within the next ${horizonLabel(result.horizon_weeks)}, bottoming out around ${short}.`;
  return `${below}, bottom out around ${short}, and stay there for ${weeksOrMonths(dip.weeksBelow)} before your paychecks catch up.`;
}

const LABELS: Record<string, string> = {
  spend: "Card purchase", doordash: "DoorDash", tmobile: "T-Mobile", car: "Car payment", rent: "Rent",
  gym: "Gym", geico: "Geico", grocer: "Groceries", insurance: "Insurance",
};

/** "Synthetic e3ed4c8012ad demo_jordan_doordash_12" -> "DoorDash"; real descriptions pass through. */
export function describeTransaction(record: Record<string, unknown>): string {
  const raw = String(record.description ?? record.merchant ?? record.type ?? "Transaction");
  if (/^Payroll\b/i.test(raw)) return "Paycheck";
  const seeded = raw.match(/^Synthetic\s+\S+\s+demo_[a-z]+_([a-z]+?)(?:_\d+)?$/i);
  if (!seeded) return raw;
  const key = seeded[1].toLowerCase();
  return LABELS[key] ?? key.charAt(0).toUpperCase() + key.slice(1);
}

export function transactionDate(record: Record<string, unknown>): string | null {
  const raw = record.purchase_date ?? record.transaction_date ?? record.payment_date ?? record.deposit_date;
  if (typeof raw !== "string") return null;
  const [y, m, d] = raw.split("-").map(Number);
  return y && m && d ? new Date(y, m - 1, d).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : raw;
}

/** odds() for a standalone stat: "Over 99%". */
export const oddsStat = (rate: number) => odds(rate).replace(/^./, c => c.toUpperCase());
