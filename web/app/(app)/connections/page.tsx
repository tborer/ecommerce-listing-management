"use client";

import { Suspense, useCallback, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import Shell from "@/components/Shell";
import { api, ApiError, fmtTime } from "@/lib/api";

type Conn = {
  cj: { connected: boolean; key_hint?: string; updated_at?: string; token_ok_at?: string | null; last_error?: string | null };
  ebay: { connected: boolean; app_configured: boolean; env: string; connected_at?: string };
  encryption_configured: boolean;
};

function ConnectionsInner() {
  const params = useSearchParams();
  const [conn, setConn] = useState<Conn | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad" | "warn"; text: string } | null>(() => {
    const e = params.get("ebay");
    if (e === "connected") return { tone: "ok", text: "eBay account connected." };
    if (e === "error") return { tone: "bad", text: "eBay connection didn't complete. Please try again." };
    return null;
  });

  const load = useCallback(() => api<Conn>("/connections").then(setConn), []);
  useEffect(() => { load(); }, [load]);

  async function run(fn: () => Promise<void>) {
    setBusy(true);
    setMsg(null);
    try { await fn(); } catch (e) { setMsg({ tone: "bad", text: e instanceof ApiError ? e.message : "Request failed" }); }
    finally { setBusy(false); load(); }
  }

  const saveKey = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      const r = await api<{ ok: boolean; error?: string }>("/connections/cj", { method: "PUT", json: { api_key: key } });
      setKey("");
      setMsg(r.ok ? { tone: "ok", text: "CJ API key saved and verified." } : { tone: "warn", text: `Key saved, but CJ rejected it: ${r.error}` });
    });
  };

  if (!conn) return <p className="muted">Loading…</p>;
  return (
    <div className="stack">
      <div>
        <h1>Connections</h1>
        <p className="muted">Credentials are encrypted before they&apos;re stored and are never shown again after saving.</p>
      </div>
      {msg && <div className={`notice ${msg.tone}`}>{msg.text}</div>}
      {!conn.encryption_configured && (
        <div className="notice bad">The server has no ELM_ENCRYPTION_KEY set, so credentials can&apos;t be saved yet.</div>
      )}

      <section className="panel">
        <div className="row">
          <h2 style={{ margin: 0 }}>CJdropshipping</h2>
          <span className={`badge ${conn.cj.connected ? (conn.cj.last_error ? "bad" : "ok") : "neutral"}`}>
            {conn.cj.connected ? (conn.cj.last_error ? "Key not working" : "Connected") : "Not connected"}
          </span>
        </div>
        <p className="small muted" style={{ marginTop: 8 }}>
          Find your API key in CJdropshipping under <b>My CJ → Authorization → API</b>. It&apos;s used to search products and quote shipping.
        </p>
        {conn.cj.connected && (
          <p className="small">
            Key {conn.cj.key_hint} · saved {fmtTime(conn.cj.updated_at)}
            {conn.cj.token_ok_at && <> · last verified {fmtTime(conn.cj.token_ok_at)}</>}
          </p>
        )}
        {conn.cj.last_error && <div className="notice bad small">{conn.cj.last_error}</div>}
        <form className="row" onSubmit={saveKey} style={{ marginTop: 8 }}>
          <input type="password" autoComplete="off" placeholder={conn.cj.connected ? "Replace API key" : "CJ API key"}
            value={key} onChange={(e) => setKey(e.target.value)} style={{ maxWidth: 420 }} />
          <button className="btn primary" disabled={busy || key.length < 8}>Save & verify</button>
          {conn.cj.connected && (
            <>
              <button type="button" className="btn" disabled={busy} onClick={() => run(async () => {
                const r = await api<{ ok: boolean; error?: string }>("/connections/cj/test", { method: "POST" });
                setMsg(r.ok ? { tone: "ok", text: "CJ connection works." } : { tone: "bad", text: r.error ?? "CJ rejected the key" });
              })}>Test</button>
              <button type="button" className="btn danger" disabled={busy} onClick={() => confirm("Remove the CJ API key?") && run(async () => {
                await api("/connections/cj", { method: "DELETE" });
              })}>Remove</button>
            </>
          )}
        </form>
      </section>

      <section className="panel">
        <div className="row">
          <h2 style={{ margin: 0 }}>eBay</h2>
          <span className={`badge ${conn.ebay.connected ? "ok" : "neutral"}`}>{conn.ebay.connected ? "Connected" : "Not connected"}</span>
          {conn.ebay.env !== "production" && <span className="badge warn">{conn.ebay.env}</span>}
        </div>
        <p className="small muted" style={{ marginTop: 8 }}>
          You&apos;ll be sent to eBay to approve access for creating listings and reading your business policies.
        </p>
        {conn.ebay.connected && conn.ebay.connected_at && <p className="small">Connected {fmtTime(conn.ebay.connected_at)}</p>}
        {!conn.ebay.app_configured && <div className="notice warn small">The server&apos;s eBay app keys aren&apos;t configured yet (EBAY_APP_ID, EBAY_CERT_ID, EBAY_RUNAME).</div>}
        <div className="row" style={{ marginTop: 8 }}>
          <button className="btn primary" disabled={busy || !conn.ebay.app_configured} onClick={() => run(async () => {
            const { url } = await api<{ url: string }>("/connections/ebay/start");
            window.location.href = url;
          })}>{conn.ebay.connected ? "Reconnect eBay" : "Connect eBay"}</button>
          {conn.ebay.connected && (
            <button className="btn danger" disabled={busy} onClick={() => confirm("Disconnect eBay?") && run(async () => {
              await api("/connections/ebay", { method: "DELETE" });
            })}>Disconnect</button>
          )}
        </div>
      </section>
    </div>
  );
}

export default function ConnectionsPage() {
  return (
    <Shell>
      <Suspense>
        <ConnectionsInner />
      </Suspense>
    </Shell>
  );
}
