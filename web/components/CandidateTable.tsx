"use client";

import { useState } from "react";
import { api, ApiError, money } from "@/lib/api";
import type { Candidate } from "@/lib/types";

const STATUS_BADGE: Record<string, [string, string]> = {
  passed: ["ok", "Meets criteria"],
  rejected: ["bad", "Fails criteria"],
  no_match: ["neutral", "No CJ match"],
  discovered: ["neutral", "Matching…"],
  listing_queued: ["warn", "Listing…"],
  listed: ["ok", "Listed"],
  list_failed: ["bad", "Listing failed"],
  dismissed: ["neutral", "Dismissed"],
};

function Thumb({ src }: { src: string }) {
  // eslint-disable-next-line @next/next/no-img-element -- remote supplier/eBay images, any host
  return <img className="thumb" src={src} alt="" loading="lazy" onError={(e) => { e.currentTarget.style.visibility = "hidden"; }} />;
}

function Rules({ c }: { c: Candidate }) {
  if (!c.criteria.length && !c.match_reasons.length) return null;
  const failing = c.criteria.filter((r) => !r.ok).length;
  return (
    <details>
      <summary className="small">
        {c.criteria.length ? (failing ? `${failing} rule${failing > 1 ? "s" : ""} failed` : "All rules pass") : "Match details"}
      </summary>
      <ul className="rules">
        {c.criteria.map((r) => (
          <li key={r.rule}>
            <span className={r.ok ? "pos" : "neg"}>{r.ok ? "✓" : "✗"}</span> {r.rule}: <span className="muted">{r.detail}</span>
          </li>
        ))}
        {c.match_reasons.map((m) => (
          <li key={m} className="muted">· {m}</li>
        ))}
      </ul>
    </details>
  );
}

export default function CandidateTable({
  items,
  onChanged,
}: {
  items: Candidate[];
  onChanged: (c: Candidate) => void;
}) {
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(c: Candidate, action: "list" | "dismiss" | "restore") {
    if (action === "list" && !confirm(`List "${c.cj_title ?? c.ebay_title}" on eBay at ${money(c.list_price ?? c.ebay_price)}?`)) return;
    setBusy(c.id);
    setError(null);
    try {
      onChanged(await api<Candidate>(`/candidates/${c.id}/${action}`, { method: "POST" }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Action failed");
    } finally {
      setBusy(null);
    }
  }

  if (!items.length) return <p className="muted">Nothing here yet.</p>;
  return (
    <>
      {error && <div className="notice bad" style={{ marginBottom: 8 }}>{error}</div>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>eBay item</th>
              <th>CJ match</th>
              <th className="num">eBay price</th>
              <th className="num">CJ cost</th>
              <th className="num">Shipping</th>
              <th className="num">Profit</th>
              <th className="num">Match</th>
              <th>Criteria</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {items.map((c) => {
              const [tone, label] = STATUS_BADGE[c.status] ?? ["neutral", c.status];
              return (
                <tr key={c.id}>
                  <td className="title-cell">
                    <div className="row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                      {c.ebay_image_url && <Thumb src={c.ebay_image_url} />}
                      <div>
                        <a className="clamp" href={c.ebay_url} target="_blank" rel="noreferrer">{c.ebay_title}</a>
                        <div className="small muted">{c.source}</div>
                      </div>
                    </div>
                  </td>
                  <td className="title-cell">
                    {c.cj_title && c.status === "no_match" ? (
                      <div className="small muted">
                        Closest CJ result:{" "}
                        <a className="clamp" href={c.cj_url ?? "#"} target="_blank" rel="noreferrer">{c.cj_title}</a>
                      </div>
                    ) : c.cj_title ? (
                      <div className="row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                        {c.cj_image_url && <Thumb src={c.cj_image_url} />}
                        <div>
                          <a className="clamp" href={c.cj_url ?? "#"} target="_blank" rel="noreferrer">{c.cj_title}</a>
                          {c.cj_variant_name && <div className="small muted">Variant: {c.cj_variant_name}</div>}
                        </div>
                      </div>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td className="num">
                    {money(c.ebay_price)}
                    {c.ebay_discount_pct ? <div className="small muted">−{c.ebay_discount_pct}%</div> : null}
                    {c.list_price != null && c.list_price !== c.ebay_price && (
                      <div className="small muted">list {money(c.list_price)}</div>
                    )}
                  </td>
                  <td className="num">{money(c.cj_cost)}</td>
                  <td className="num">
                    {money(c.shipping_cost)}
                    {c.shipping_days_max != null && <div className="small muted">≤{c.shipping_days_max}d · {c.shipping_method}</div>}
                  </td>
                  <td className="num">
                    {c.profit != null ? (
                      <>
                        <span className={c.profit >= 0 ? "pos" : "neg"}>{money(c.profit)}</span>
                        <div className="small muted">{c.margin_pct}%</div>
                      </>
                    ) : "—"}
                  </td>
                  <td className="num">{c.match_score ?? "—"}</td>
                  <td>
                    <span className={`badge ${tone}`}>{label}</span>
                    {c.auto_listed && <span className="badge neutral" style={{ marginLeft: 4 }}>auto</span>}
                    <Rules c={c} />
                    {c.error && <div className="small neg" style={{ maxWidth: 260 }}>{c.error}</div>}
                  </td>
                  <td>
                    <div className="row" style={{ flexDirection: "column", alignItems: "stretch" }}>
                      {(c.status === "passed" || c.status === "list_failed") && (
                        <button className="btn primary sm" disabled={busy === c.id} onClick={() => act(c, "list")}>
                          {busy === c.id ? "Listing…" : c.status === "list_failed" ? "Retry listing" : "List on eBay"}
                        </button>
                      )}
                      {c.status === "rejected" && c.cj_cost != null && (
                        <button className="btn sm" disabled={busy === c.id} onClick={() => act(c, "list")}>
                          List anyway
                        </button>
                      )}
                      {c.status === "listed" && c.ebay_listing_url && (
                        <a className="btn sm" href={c.ebay_listing_url} target="_blank" rel="noreferrer">View listing</a>
                      )}
                      {!["listed", "listing_queued", "dismissed"].includes(c.status) && (
                        <button className="btn sm" disabled={busy === c.id} onClick={() => act(c, "dismiss")}>Dismiss</button>
                      )}
                      {c.status === "dismissed" && (
                        <button className="btn sm" disabled={busy === c.id} onClick={() => act(c, "restore")}>Restore</button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </>
  );
}
