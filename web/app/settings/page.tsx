"use client";

import { useEffect, useState } from "react";
import Shell from "@/components/Shell";
import { api, ApiError, hourLabel, localHourToUtc, utcHourToLocal } from "@/lib/api";
import type { Settings } from "@/lib/types";

type Option = { value: string; label: string };
type Policies = Record<"fulfillment" | "payment" | "return", { id: string; name: string }[]>;

function Num({ label, hint, value, onChange, step = 1, min = 0, max }: {
  label: string; hint?: string; value: number; onChange: (v: number) => void; step?: number; min?: number; max?: number;
}) {
  return (
    <label className="field">
      {label}
      <input type="number" value={Number.isFinite(value) ? value : ""} step={step} min={min} max={max}
        onChange={(e) => onChange(e.target.valueAsNumber)} />
      {hint && <span className="hint">{hint}</span>}
    </label>
  );
}

function SettingsInner() {
  const [s, setS] = useState<Settings | null>(null);
  const [options, setOptions] = useState<Option[]>([]);
  const [keywords, setKeywords] = useState("");
  const [policies, setPolicies] = useState<Policies | null>(null);
  const [policyError, setPolicyError] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ tone: "ok" | "bad"; text: string } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api<{ settings: Settings; options: { deal_categories: Option[] } }>("/settings").then((r) => {
      setS(r.settings);
      setOptions(r.options.deal_categories);
      setKeywords(r.settings.keywords.join("\n"));
    });
    api<Policies>("/ebay/policies").then(setPolicies).catch((e) => setPolicyError(e instanceof ApiError ? e.message : "Couldn't load eBay policies"));
  }, []);

  if (!s) return <p className="muted">Loading…</p>;
  const set = <K extends keyof Settings>(k: K, v: Settings[K]) => setS({ ...s, [k]: v });

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setMsg(null);
    try {
      const body = { ...s, keywords: keywords.split("\n").map((k) => k.trim()).filter(Boolean) };
      const r = await api<{ settings: Settings }>("/settings", { method: "PUT", json: body });
      setS(r.settings);
      setKeywords(r.settings.keywords.join("\n"));
      setMsg({ tone: "ok", text: "Saved." });
    } catch (err) {
      setMsg({ tone: "bad", text: err instanceof ApiError ? err.message : "Couldn't save" });
    } finally {
      setSaving(false);
    }
  }

  const toggleCategory = (value: string, on: boolean) =>
    set("deal_categories", on ? [...s.deal_categories, value] : s.deal_categories.filter((c) => c !== value));

  const policySelect = (kind: keyof Policies, key: "ebay_fulfillment_policy_id" | "ebay_payment_policy_id" | "ebay_return_policy_id", label: string) => (
    <label className="field">
      {label}
      <select value={s[key] ?? ""} onChange={(e) => set(key, e.target.value || null)} disabled={!policies}>
        <option value="">— choose —</option>
        {(policies?.[kind] ?? []).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
      </select>
    </label>
  );

  return (
    <form className="settings" onSubmit={save}>
      <div className="row">
        <div>
          <h1>Settings</h1>
          <p className="muted">What to look for on eBay, what counts as a good CJ match, and when to run.</p>
        </div>
        <span className="spacer" />
        <button className="btn primary" disabled={saving}>{saving ? "Saving…" : "Save settings"}</button>
      </div>
      {msg && <div className={`notice ${msg.tone}`}>{msg.text}</div>}

      <section className="panel">
        <h2>Where to look on eBay</h2>
        <p className="small muted">eBay deal categories (via eBay&apos;s Deal API) and/or your own search keywords.</p>
        <div className="checks">
          {options.map((o) => (
            <label key={o.value} className="check">
              <input type="checkbox" checked={s.deal_categories.includes(o.value)} onChange={(e) => toggleCategory(o.value, e.target.checked)} />
              {o.label}
            </label>
          ))}
        </div>
        <div className="fields" style={{ marginTop: 12 }}>
          <label className="field">
            Search keywords
            <textarea value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder={"one per line, e.g.\nneck fan\nled strip lights"} />
          </label>
          <Num label="Items per run" hint="New eBay items to check each run" value={s.items_per_run} min={1} max={200} onChange={(v) => set("items_per_run", v)} />
          <Num label="Min eBay price ($)" value={s.min_ebay_price} step={0.01} onChange={(v) => set("min_ebay_price", v)} />
          <Num label="Max eBay price ($)" value={s.max_ebay_price} step={0.01} onChange={(v) => set("max_ebay_price", v)} />
        </div>
      </section>

      <section className="panel">
        <h2>Criteria an item must meet</h2>
        <div className="fields">
          <Num label="eBay fees (%)" hint="Final value + payment fees estimate" value={s.fee_pct} step={0.1} onChange={(v) => set("fee_pct", v)} />
          <Num label="Target margin (%)" hint="Profit you want left after fees, cost and shipping" value={s.target_margin_pct} step={0.1} onChange={(v) => set("target_margin_pct", v)} />
          <Num label="Minimum profit ($)" value={s.min_profit} step={0.01} onChange={(v) => set("min_profit", v)} />
          <Num label="Max CJ shipping cost ($)" value={s.max_shipping_cost} step={0.01} onChange={(v) => set("max_shipping_cost", v)} />
          <Num label="Max delivery time (days)" value={s.max_delivery_days} min={1} max={90} onChange={(v) => set("max_delivery_days", v)} />
          <Num label="Min match score (0-100)" hint="50+ is a confident title match" value={s.min_match_score} max={100} onChange={(v) => set("min_match_score", v)} />
          <label className="field">
            CJ warehouse
            <select value={s.cj_warehouse} onChange={(e) => set("cj_warehouse", e.target.value as Settings["cj_warehouse"])}>
              <option value="any">Any (usually ships from China)</option>
              <option value="US">US warehouse only (faster, fewer products)</option>
            </select>
          </label>
          <Num label="Undercut eBay price (%)" hint="List this much below the eBay item's price" value={s.price_undercut_pct} step={0.5} max={50} onChange={(v) => set("price_undercut_pct", v)} />
        </div>
      </section>

      <section className="panel">
        <h2>Schedule</h2>
        <div className="fields">
          <label className="check">
            <input type="checkbox" checked={s.schedule_enabled} onChange={(e) => set("schedule_enabled", e.target.checked)} />
            Run automatically once a day
          </label>
          <label className="field">
            Time (your local time)
            <select value={utcHourToLocal(s.schedule_hour_utc)} onChange={(e) => set("schedule_hour_utc", localHourToUtc(Number(e.target.value)))}>
              {Array.from({ length: 24 }, (_, h) => <option key={h} value={h}>{hourLabel(h)}</option>)}
            </select>
            <span className="hint">Runs at the first scheduler tick after this time. On Vercel&apos;s free plan the scheduler ticks once a day.</span>
          </label>
        </div>
      </section>

      <section className="panel">
        <h2>Listing on eBay</h2>
        <div className="fields">
          <label className="check">
            <input type="checkbox" checked={s.auto_list_enabled} onChange={(e) => set("auto_list_enabled", e.target.checked)} />
            Auto-list items that meet every criterion
          </label>
          <Num label="Max auto-listings per run" hint="Most profitable first" value={s.auto_list_max_per_run} max={25} onChange={(v) => set("auto_list_max_per_run", v)} />
        </div>
        <h2 style={{ marginTop: 16 }}>eBay business policies</h2>
        {policyError && <div className="notice warn small">{policyError}</div>}
        <div className="fields">
          {policySelect("fulfillment", "ebay_fulfillment_policy_id", "Shipping policy")}
          {policySelect("payment", "ebay_payment_policy_id", "Payment policy")}
          {policySelect("return", "ebay_return_policy_id", "Return policy")}
        </div>
        <h2 style={{ marginTop: 16 }}>Item location</h2>
        <p className="small muted">Shown to buyers as where the item ships from (city level is fine).</p>
        <div className="fields">
          <label className="field">City<input type="text" value={s.location_city ?? ""} onChange={(e) => set("location_city", e.target.value || null)} /></label>
          <label className="field">State<input type="text" value={s.location_state ?? ""} onChange={(e) => set("location_state", e.target.value || null)} /></label>
          <label className="field">ZIP<input type="text" value={s.location_postal_code ?? ""} onChange={(e) => set("location_postal_code", e.target.value || null)} /></label>
          <label className="field">Country<input type="text" value={s.location_country} maxLength={2} onChange={(e) => set("location_country", e.target.value.toUpperCase())} /></label>
        </div>
      </section>
    </form>
  );
}

export default function SettingsPage() {
  return (
    <Shell>
      <SettingsInner />
    </Shell>
  );
}
