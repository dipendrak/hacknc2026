"use client";

import * as React from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, askDecision, fetchCustomers, fetchModel, fetchRecentRuns } from "@/lib/api";
import type { Customer, CashflowModel, SavedReplay } from "@/lib/types";
import { ArrowRight, ArrowUpRight, Landmark, ChartNoAxesColumnIncreasing, History, ChevronRight, Check, Plus, PencilLine, RotateCcw } from "lucide-react";
import { AppShell, type WorkspaceView } from "./app-shell";
import { AeroButton, AeroPanel, GlassLayers } from "@/components/ui/aero";
import { useSession } from "@/lib/auth";
import { displayName } from "@/lib/summary";

const STORAGE_KEY = "counterfactual.workspace.v1";
const EXAMPLES = ["Can I afford $1400 today?", "Can I afford $65/month starting today?", "Can I afford $1200 in 30 days?"];
type Draft = { id: string; text: string; createdAt: string };
type Preferences = { accent: "ocean" | "meadow"; transparent: boolean; background: "water" | "city" | "globe" | "flight" };
const DEFAULT_PREFERENCES: Preferences = { accent: "ocean", transparent: true, background: "water" };
const BACKGROUNDS = [
  { id: "water", name: "Water world", src: "/images/aero-water-world.png" },
  { id: "city", name: "Green city", src: "/images/aero-green-city.jpg" },
  { id: "globe", name: "Glass globe", src: "/images/aero-globe-city.jpg" },
  { id: "flight", name: "Open skies", src: "/images/aero-globe-flight.jpg" },
] as const;

// The answer's opening sentence ("Spending $400 today looks manageable."), so repeated questions are told apart.
function verdictLine(prose: string): string {
  const first = prose.split(/\n/)[0]?.trim() ?? "";
  return first.length > 140 ? `${first.slice(0, 137).trimEnd()}…` : first;
}

function askedAt(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

export function Workspace({ view }: { view: WorkspaceView | null }) {
  const router = useRouter();
  const session = useSession();
  const signedInCustomer = session?.user.customerId;
  const [customers, setCustomers] = React.useState<Customer[]>([]);
  const [customerId, setCustomerId] = React.useState("");
  const [model, setModel] = React.useState<CashflowModel | null>(null);
  const [connectionError, setConnectionError] = React.useState("");
  const [modelError, setModelError] = React.useState("");
  const [connecting, setConnecting] = React.useState(true);
  const [pending, setPending] = React.useState(false);
  const [requestStatus, setRequestStatus] = React.useState("");
  const [recent, setRecent] = React.useState<SavedReplay[]>([]);
  // Replays the API saved for this person from any channel (texts included), not just this browser.
  const [serverRecent, setServerRecent] = React.useState<SavedReplay[]>([]);
  const submitting = React.useRef(false);
  const [connectionVersion, setConnectionVersion] = React.useState(0);
  const [drafts, setDrafts] = React.useState<Draft[]>([]);
  const [text, setText] = React.useState("");
  const [runId, setRunId] = React.useState("");
  const [preferences, setPreferences] = React.useState<Preferences>(DEFAULT_PREFERENCES);
  const [loaded, setLoaded] = React.useState(false);
  const [message, setMessage] = React.useState("");
  const [error, setError] = React.useState("");
  const [runError, setRunError] = React.useState("");
  const [example, setExample] = React.useState(0);
  const [removed, setRemoved] = React.useState<Draft | null>(null);
  const textarea = React.useRef<HTMLTextAreaElement>(null);

  React.useEffect(() => {
    if (!signedInCustomer) return;
    const controller = new AbortController();
    fetchRecentRuns(signedInCustomer, controller.signal).then(runs => setServerRecent(runs.map(r => ({
      run_id: r.run_id, question: r.question, prose: r.verdict, createdAt: r.created_at,
      customer: session?.user.name ?? "", customer_id: signedInCustomer,
    })))).catch(() => { /* The browser's own history still shows. */ });
    return () => controller.abort();
  }, [signedInCustomer, session?.user.name, recent.length]);

  // The workspace needs a sign-in; replay links (/r/...) stay open to anyone with the link.
  React.useEffect(() => {
    if (session === null) router.replace(`/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`);
  }, [session, router]);

  React.useEffect(() => {
    if (!loaded || !signedInCustomer) return;
    const controller = new AbortController();
    setConnecting(true); setConnectionError("");
    fetchCustomers(controller.signal).then(all => {
      // Signed-in users only see their own profile.
      const items = all.filter(item => item.customer_id === signedInCustomer);
      if (!items.length) throw new Error("Your account's bank profile isn't available right now.");
      setCustomers(items);
      setCustomerId(current => items.some(item => item.customer_id === current) ? current : items.find(item => item.name.includes("Alex"))?.customer_id ?? items[0]?.customer_id ?? "");
    }).catch(error => { if (!controller.signal.aborted) { setConnectionError(error instanceof Error ? error.message : "Profiles could not load."); setCustomers([]); setCustomerId(""); } })
      .finally(() => { if (!controller.signal.aborted) setConnecting(false); });
    return () => controller.abort();
  }, [connectionVersion, loaded, signedInCustomer]);

  React.useEffect(() => {
    setModel(null); setModelError("");
    if (!customerId) return;
    const controller = new AbortController();
    fetchModel(customerId, controller.signal).then(setModel).catch(error => { if (!controller.signal.aborted) setModelError(error instanceof Error ? error.message : "This profile could not load."); });
    return () => controller.abort();
  }, [customerId, connectionVersion]);

  React.useEffect(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "null");
      if (saved && typeof saved === "object") {
        if (Array.isArray(saved.drafts)) setDrafts(saved.drafts.filter((draft: unknown): draft is Draft => {
          if (!draft || typeof draft !== "object") return false;
          const d = draft as Partial<Draft>;
          return typeof d.id === "string" && typeof d.text === "string" && typeof d.createdAt === "string" && Number.isFinite(Date.parse(d.createdAt));
        }).slice(0, 100));
        if (saved.preferences?.accent === "ocean" || saved.preferences?.accent === "meadow") {
          setPreferences({ accent: saved.preferences.accent, transparent: saved.preferences.transparent !== false, background: saved.wallpaperVersion === 2 && (saved.preferences.background === "city" || saved.preferences.background === "globe" || saved.preferences.background === "flight") ? saved.preferences.background : "water" });
        }
        if (typeof saved.customerId === "string") setCustomerId(saved.customerId);
        if (Array.isArray(saved.recent)) setRecent(saved.recent.filter((item: SavedReplay) => typeof item?.run_id === "string" && typeof item.question === "string" && typeof item.prose === "string" && typeof item.customer === "string").slice(0, 30));
        if (typeof saved.composer === "string") setText(saved.composer.slice(0, 1000));
      }
    } catch { setMessage("Browser storage is unavailable. Drafts will stay in this session."); }
    setLoaded(true);
  }, []);

  React.useEffect(() => {
    if (!loaded) return;
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ drafts, preferences, composer: text, wallpaperVersion: 2, customerId, recent })); }
    catch { setMessage("Changes could not be saved to this browser. They are still available in this session."); }
  }, [drafts, preferences, text, loaded, customerId, recent]);

  function saveDraft() {
    if (!text.trim()) { setError("Add a decision to save your draft."); textarea.current?.focus(); return; }
    if (drafts.length >= 100) { setError("Your workspace has 100 drafts. Remove one before saving another."); return; }
    setDrafts(current => [{ id: crypto.randomUUID(), text: text.trim(), createdAt: new Date().toISOString() }, ...current]);
    setText(""); setError(""); setMessage("Draft saved. You can find it in Scenarios.");
  }

  async function runDecision(event: React.FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    if (!text.trim()) { setError("Add an amount and timing to your question."); textarea.current?.focus(); return; }
    if (connecting || !customerId || !model || model.customer_id !== customerId) { setError("Select an available demo profile first."); return; }
    const question = text.trim();
    submitting.current = true; setPending(true); setError(""); setRequestStatus("Exploring possible balances…");
    const timer = window.setTimeout(() => setRequestStatus("Still working on your question…"), 4000);
    try {
      const response = await askDecision(question, customerId);
      if (response.validated !== true || !response.run_id) throw new Error("The service returned an unchecked result. Your question has been kept.");
      const entry: SavedReplay = { run_id: response.run_id, question, customer: customers.find(c => c.customer_id === customerId)?.name ?? customerId, customer_id: customerId, prose: response.prose, createdAt: new Date().toISOString() };
      const next = [entry, ...recent].slice(0, 30);
      setRecent(next);
      try { localStorage.setItem(STORAGE_KEY, JSON.stringify({ drafts, preferences, composer: text, wallpaperVersion: 2, customerId, recent: next })); } catch { /* Saved API replay remains available without local history. */ }
      router.push(`/r/${encodeURIComponent(response.run_id)}`);
    } catch (error) {
      setError(error instanceof Error ? error.message : "The request failed. Your question has been kept; you can retry.");
      if (error instanceof ApiError && error.status === 404) setConnectionVersion(n => n + 1);
      textarea.current?.focus();
    } finally { window.clearTimeout(timer); submitting.current = false; setPending(false); setRequestStatus(""); }
  }
  // Only the signed-in person's replays (this history is per browser, shared by everyone who signs in on it).
  const local = recent.filter(run => run.customer_id ? run.customer_id === signedInCustomer
    : displayName(run.customer) === session?.user.name);
  const seen = new Set<string>();
  const mine = [...serverRecent, ...local]
    .filter(run => !seen.has(run.run_id) && seen.add(run.run_id))
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt)).slice(0, 30);
  const recentList = mine.length > 0 && <AeroPanel className="result-panel"><h2>Recent simulations</h2><ul className="replay-history">{mine.map(run => <li key={run.run_id}><Link href={`/r/${encodeURIComponent(run.run_id)}`}>{run.question}</Link><span className="replay-verdict">{verdictLine(run.prose)}</span><span className="small-copy">{askedAt(run.createdAt)}</span></li>)}</ul></AeroPanel>;

  const draftList = (
    <AeroPanel className="scenario-panel" aria-labelledby="scenarios-heading">
      <div className="panel-heading"><h2 id="scenarios-heading">Your scenarios</h2>{drafts.length > 0 && <span className="small-copy">{drafts.length} saved</span>}</div>
      {!loaded ? <div className="empty-state"><div className="loading-placeholder" /><p>Loading your workspace…</p></div> : drafts.length === 0 ? <div className="empty-state"><GlassLayers /><h3>A fresh start</h3><p>Saved drafts will appear here.</p></div> :
        <ul className="draft-list">{drafts.map(draft => <li key={draft.id}><span className="draft-icon"><PencilLine size={18} strokeWidth={1.5} aria-hidden="true" /></span><div><h3>{draft.text}</h3><p>{new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" }).format(new Date(draft.createdAt))} · Draft</p></div><button type="button" className="text-button" onClick={() => { setText(draft.text); router.push("/?view=overview"); }}>Use draft</button><button type="button" className="text-button remove-draft" onClick={() => { setDrafts(current => current.filter(d => d.id !== draft.id)); setRemoved(draft); setMessage("Draft removed. Use Undo to restore it."); }} aria-label={`Remove draft: ${draft.text}`}>Remove</button></li>)}</ul>}
    </AeroPanel>
  );

  const composer = <AeroPanel className="composer-panel"><form onSubmit={runDecision} aria-busy={pending}>
    <label className="panel-title" htmlFor="customer-profile">Your profile</label>
    <select id="customer-profile" value={customerId} onChange={event => setCustomerId(event.target.value)} disabled={pending || connecting || customers.length <= 1}>
      <option value="" disabled>{connecting ? "Loading profiles…" : customers.length ? "Choose a profile" : "No profiles available"}</option>
      {customers.map(customer => <option key={customer.customer_id} value={customer.customer_id}>{displayName(customer.name)}</option>)}
    </select>
    <p className="small-copy">Cached synthetic bank profiles; no live bank account is connected.</p>
    {connectionError && <p className="form-error" role="alert">{connectionError}</p>}
    {!connecting && !customers.length && !connectionError && <p>No demo profiles are cached. Start the API with an ingested population.</p>}
    {(connectionError || !customers.length) && !connecting && <AeroButton type="button" variant="secondary" onClick={() => setConnectionVersion(n => n + 1)}>Retry connection</AeroButton>}
    {modelError && <p className="form-error" role="alert">{modelError} Choose another profile.</p>}
    {customerId && !model && !modelError && <p className="small-copy">Loading this profile…</p>}

    <label className="panel-title" htmlFor="decision">What are you thinking about?</label>
    <textarea ref={textarea} id="decision" name="decision" autoComplete="off" maxLength={1000} rows={3} value={text} onChange={event => { setText(event.target.value); setError(""); }} placeholder="Can I afford $1400 today?" disabled={pending} aria-invalid={Boolean(error)} aria-describedby={error ? "decision-error" : "draft-note"} />
    {error && <p id="decision-error" className="form-error" role="alert">{error}</p>}
    <div className="composer-actions"><span id="draft-note" className="small-copy">Include an amount and timing.</span><div><AeroButton type="button" variant="quiet" disabled={pending} onClick={() => { setText(EXAMPLES[example % EXAMPLES.length]); setExample(n => n + 1); setError(""); textarea.current?.focus(); }}>Try an example</AeroButton><AeroButton type="button" variant="secondary" onClick={saveDraft} disabled={!loaded || pending}>Save draft <span className="button-icon"><ArrowRight size={17} strokeWidth={1.5} aria-hidden="true" /></span></AeroButton><AeroButton type="submit" disabled={pending || connecting || !model || model.customer_id !== customerId || !loaded}>{pending ? "Running…" : "Run simulation"}</AeroButton></div></div><p className="request-status" role="status" aria-live="polite">{requestStatus}</p>
  </form></AeroPanel>;

  const renderView = (view: WorkspaceView) => <>
    {view === "overview" || view === "scenarios" ? <>
      <div className="page-heading"><h1>{view === "overview" ? "A little clarity for what comes next." : "Make space for your next idea."}</h1><p>{view === "overview" ? "Make room for your next decision." : "Run a saved draft or revisit a simulation."}</p></div>
      {view === "overview" && composer}
      <div className="overview-grid">{draftList}<AeroPanel className="workspace-panel"><h2>Your workspace</h2><div className="workspace-status"><span className="status-icon"><Landmark size={22} strokeWidth={1.5} aria-hidden="true" /></span><div><h3>Bank profile</h3><p>{model ? displayName(customers.find(c => c.customer_id === customerId)?.name ?? "Profile loaded") : modelError ? "Profile unavailable" : connecting ? "Connecting…" : connectionError ? "API unavailable" : "Loading profile…"}</p></div></div><div className="workspace-status"><span className="status-icon"><ChartNoAxesColumnIncreasing size={22} strokeWidth={1.5} aria-hidden="true" /></span><div><h3>Simulation</h3><p>{mine.length ? `${mine.length} saved replay${mine.length === 1 ? "" : "s"}` : "Not run yet"}</p></div></div><Link className="workspace-action" href="/?view=replays"><History size={20} strokeWidth={1.5} aria-hidden="true" />Open a replay<ChevronRight size={17} strokeWidth={1.5} aria-hidden="true" /></Link></AeroPanel></div>
      {recentList}
    </> : view === "replays" ? <>
      <div className="page-heading"><h1>A second look at a decision.</h1><p>Open a saved simulation using its replay ID.</p></div>
      <AeroPanel className="replay-form"><h2>Open a replay</h2><p className="supporting-copy">Use the ID from a simulation response or a shared replay link.</p><form action={runId.trim() ? `/r/${encodeURIComponent(runId.trim())}` : undefined} onSubmit={event => { if (!runId.trim()) { event.preventDefault(); setRunError("Enter a replay ID to continue."); document.getElementById("run-id")?.focus(); } }}><label htmlFor="run-id">Replay ID</label><div className="input-action"><input id="run-id" name="run_id" autoComplete="off" spellCheck={false} placeholder="e.g. fixture-run-0001…" value={runId} onChange={event => { setRunId(event.target.value); setRunError(""); }} maxLength={200} aria-invalid={Boolean(runError)} aria-describedby={runError ? "run-error" : undefined} /><AeroButton type="submit">Open replay<ArrowUpRight size={17} strokeWidth={1.5} aria-hidden="true" /></AeroButton></div>{runError && <p id="run-error" className="form-error" role="alert">{runError}</p>}</form></AeroPanel>
      {recentList}
      <div className="inline-note"><History size={18} strokeWidth={1.5} aria-hidden="true" /><p>Replays show simulation results. Local drafts are available in <Link href="/?view=scenarios">Scenarios</Link>.</p></div>
    </> : <>
      <div className="page-heading"><h1>A workspace that feels like you.</h1><p>A few simple ways to make this space your own.</p></div>
      <AeroPanel className="preferences-panel"><h2>Appearance</h2><fieldset><legend>Accent color</legend><p className="supporting-copy">A touch of sky or a little green.</p><div className="accent-options">{(["ocean", "meadow"] as const).map(accent => <label key={accent} className="accent-option" data-selected={preferences.accent === accent}><input type="radio" name="accent" value={accent} checked={preferences.accent === accent} onChange={() => setPreferences(current => ({ ...current, accent }))} /><span className={`accent-swatch accent-swatch--${accent}`} aria-hidden="true" /><span>{accent === "ocean" ? "Ocean blue" : "Meadow green"}</span>{preferences.accent === accent && <Check size={16} aria-hidden="true" />}</label>)}</div></fieldset><fieldset className="background-settings"><legend>Background</legend><p className="supporting-copy">Choose one of your Frutiger Aero images.</p><div className="background-options">{BACKGROUNDS.map(background => <label key={background.id} className="background-option" data-selected={preferences.background === background.id}><img src={background.src} width={140} height={90} alt="" /><span><input type="radio" name="background" value={background.id} checked={preferences.background === background.id} onChange={() => setPreferences(current => ({ ...current, background: background.id }))} />{background.name}</span></label>)}</div></fieldset><div className="setting-row"><div><label htmlFor="glass">Glass surfaces</label><p>Let a little of the background shine through.</p></div><input className="glass-switch" type="checkbox" role="switch" id="glass" checked={preferences.transparent} onChange={event => setPreferences(current => ({ ...current, transparent: event.target.checked }))} /></div><div className="preference-footer"><span className="small-copy">Preferences stay in this browser.</span><AeroButton type="button" variant="secondary" onClick={() => { setPreferences(DEFAULT_PREFERENCES); setMessage("Appearance reset to ocean blue, glass surfaces, and the water world background."); }}><RotateCcw size={15} aria-hidden="true" />Reset appearance</AeroButton></div></AeroPanel>
      <AeroPanel className="about-panel"><span className="about-icon"><Plus size={20} strokeWidth={1.5} aria-hidden="true" /></span><div><h2>Room to build</h2><p>This workspace is the starting point. Cached profiles and simulations use the local API. Appearance and drafts stay in this browser.</p></div></AeroPanel>
    </>}
    <div className="workspace-message" role="status" aria-live="polite">{message}{removed && <button type="button" className="text-button" onClick={() => { setDrafts(current => [removed, ...current]); setRemoved(null); setMessage("Draft restored."); }}>Undo</button>}</div>
  </>;
  // Hold the desktop until the session is known, so signed-out users never see it.
  if (!session) return <div className="aero-desktop xp-desktop" data-background={preferences.background} aria-busy="true" />;
  return <AppShell view={view} accent={preferences.accent} transparent={preferences.transparent} background={preferences.background} renderView={renderView} />;
}
