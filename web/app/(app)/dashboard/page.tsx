"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import CandidateTable from "@/components/CandidateTable";
import Shell from "@/components/Shell";
import { api, ApiError, fmtTime, hourLabel, utcHourToLocal } from "@/lib/api";
import type { Candidate, Dashboard as DashboardData, Run, Settings } from "@/lib/types";

const VIEWS: { key: string; label: string; statuses: string[] }[] = [
  { key: "review", label: "To review", statuses: ["passed"] },
  { key: "listed", label: "Listed", statuses: ["listed", "listing_queued"] },
  { key: "failed", label: "Listing failed", statuses: ["list_failed"] },
  { key: "rejected", label: "Didn't qualify", statuses: ["rejected", "no_match"] },
  { key: "dismissed", label: "Dismissed", statuses: ["dismissed"] },
  { key: "all", label: "All", statuses: [] },
];

function RunSummary({ run }: { run: Run }) {
  const s = run.stats;
  const n = (k: string) => (typeof s[k] === "number" ? (s[k] as number) : 0);
  return (
    <div className="small">
      <div>
        <span className={`badge ${run.status === "error" ? "bad" : run.status === "running" ? "warn" : "ok"}`}>
          {run.status === "running" ? `Running · ${run.phase}` : run.status}
        </span>{" "}
        <span className="muted">{run.trigger} · {fmtTime(run.started_at)}</span>
      </div>
      <div className="muted" style={{ marginTop: 4 }}>
        {n("new_candidates")} new items · {n("status_passed")} meet criteria · {n("status_rejected")} fail ·{" "}
        {n("status_no_match")} no match{n("auto_list_attempted") ? ` · ${n("auto_list_attempted")} auto-listed` : ""}
      </div>
      {run.error && <div className="neg" style={{ marginTop: 4 }}>{run.error}</div>}
      {Array.isArray(s.errors) && s.errors.length > 0 && (
        <details style={{ marginTop: 4 }}>
          <summary>{s.errors.length} warning(s)</summary>
          <ul className="rules">{(s.errors as string[]).map((e, i) => <li key={i}>{e}</li>)}</ul>
        </details>
      )}
    </div>
  );
}

function DashboardInner() {
  const [dash, setDash] = useState<DashboardData | null>(null);
  const [view, setView] = useState("review");
  const [items, setItems] = useState<Candidate[]>([]);
  const [total, setTotal] = useState(0);
  const [running, setRunning] = useState<Run | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [savingAuto, setSavingAuto] = useState(false);
  const polling = useRef(false);

  const loadDash = useCallback(() => api<DashboardData>("/dashboard").then(setDash).catch(() => {}), []);
  const loadItems = useCallback(
    (v: string) =>
      api<{ total: number; items: Candidate[] }>(`/candidates?view=${v}`)
        .then((r) => { setItems(r.items); setTotal(r.total); })
        .catch(() => {}),
    [],
  );

  const drive = useCallback(
    async (run: Run) => {
      if (polling.current) return;
      polling.current = true;
      setRunning(run);
      try {
        let current = run;
        while (current.status === "running") {
          current = await api<Run>(`/runs/${current.id}/continue`, { method: "POST" });
          setRunning(current);
          loadItems(view);
        }
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "Run failed");
      } finally {
        polling.current = false;
        setRunning(null);
        loadDash();
        loadItems(view);
      }
    },
    [loadDash, loadItems, view],
  );

  useEffect(() => { loadDash(); }, [loadDash]);
  useEffect(() => { loadItems(view); }, [view, loadItems]);
  useEffect(() => {
    if (dash?.active_run && !polling.current) drive(dash.active_run);
  }, [dash?.active_run, drive]);

  async function runNow() {
    setError(null);
    try {
      const run = await api<Run>("/runs", { method: "POST" });
      if (run.status === "running") await drive(run);
      else { loadDash(); loadItems(view); if (run.status === "error") setError(run.error); }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't start a run");
    }
  }

  async function toggleAutoList(enabled: boolean) {
    setSavingAuto(true);
    try {
      const { settings } = await api<{ settings: Settings }>("/settings");
      await api("/settings", { method: "PUT", json: { ...settings, auto_list_enabled: enabled } });
      await loadDash();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Couldn't update auto-list");
    } finally {
      setSavingAuto(false);
    }
  }

  function onChanged(c: Candidate) {
    setItems((prev) => {
      const statuses = VIEWS.find((v) => v.key === view)?.statuses ?? [];
      return statuses.length && !statuses.includes(c.status) ? prev.filter((p) => p.id !== c.id) : prev.map((p) => (p.id === c.id ? c : p));
    });
    loadDash();
  }

  if (!dash) return <p className="muted">Loading…</p>;
  const count = (statuses: string[]) =>
    statuses.length ? statuses.reduce((a, s) => a + (dash.counts[s] ?? 0), 0) : Object.values(dash.counts).reduce((a, b) => a + b, 0);
  const shownRun = running ?? dash.active_run ?? dash.last_run;

  return (
    <div className="stack">
      <div className="row">
        <div>
          <h1>Dashboard</h1>
          <p className="muted">eBay items with a matching CJdropshipping product, checked against your criteria.</p>
        </div>
        <span className="spacer" />
        <button className="btn primary" onClick={runNow} disabled={!!running || !dash.connections.cj}>
          {running ? "Running…" : "Run now"}
        </button>
      </div>

      {!dash.connections.cj && (
        <div className="notice warn">Add your CJdropshipping API key on <Link href="/connections">Connections</Link> to start finding matches.</div>
      )}
      {!dash.connections.ebay && (
        <div className="notice warn">Connect your eBay account on <Link href="/connections">Connections</Link> to list items.</div>
      )}
      {error && <div className="notice bad">{error}</div>}

      <div className="grid">
        <div className="panel">
          <h2>{running ? "Current run" : "Last run"}</h2>
          {shownRun ? <RunSummary run={shownRun} /> : <p className="muted">No runs yet — click Run now.</p>}
        </div>
        <div className="panel">
          <h2>Schedule</h2>
          {dash.schedule.enabled ? (
            <p>Daily at {hourLabel(utcHourToLocal(dash.schedule.hour_utc))}<br />
              <span className="small muted">Next: {fmtTime(dash.schedule.next_slot_at)} (at the next scheduler tick after this time)</span></p>
          ) : (
            <p className="muted">Off — runs only when you click Run now.</p>
          )}
          <Link className="small" href="/settings">Change schedule & criteria</Link>
        </div>
        <div className="panel">
          <h2>Auto-list on eBay</h2>
          <label className="check">
            <input type="checkbox" checked={dash.auto_list.enabled} disabled={savingAuto}
              onChange={(e) => toggleAutoList(e.target.checked)} />
            {dash.auto_list.enabled ? `On — top ${dash.auto_list.max_per_run} per run by profit` : "Off — you review and list each item"}
          </label>
          <p className="small muted" style={{ marginTop: 6 }}>Only items that meet every criterion are ever auto-listed.</p>
        </div>
        <div className="panel">
          <h2>Totals</h2>
          <div className="row">
            <div><div className="stat">{count(["passed"])}</div><div className="small muted">to review</div></div>
            <span className="spacer" />
            <div><div className="stat">{count(["listed"])}</div><div className="small muted">listed</div></div>
            <span className="spacer" />
            <div><div className="stat">{count([])}</div><div className="small muted">found</div></div>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="tabs">
          {VIEWS.map((v) => (
            <button key={v.key} className={view === v.key ? "active" : ""} onClick={() => setView(v.key)}>
              {v.label} ({count(v.statuses)})
            </button>
          ))}
        </div>
        <CandidateTable items={items} onChanged={onChanged} />
        {total > items.length && <p className="small muted">Showing {items.length} of {total}.</p>}
      </div>
    </div>
  );
}

export default function DashboardPage() {
  return (
    <Shell>
      <DashboardInner />
    </Shell>
  );
}
