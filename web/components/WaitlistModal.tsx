"use client";

import { useEffect, useRef, useState } from "react";
import { CONSENT_TEXT } from "@/lib/brand";

type State = { kind: "idle" } | { kind: "sending" } | { kind: "done" } | { kind: "error"; message: string };

// One modal for the whole page. Any element with `data-waitlist-open` opens
// it, and so does a link to /#waitlist, so the CTA buttons can stay
// server-rendered with no JavaScript of their own.
export default function WaitlistModal() {
  const dialog = useRef<HTMLDialogElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const [state, setState] = useState<State>({ kind: "idle" });

  useEffect(() => {
    const open = () => {
      if (!dialog.current?.open) dialog.current?.showModal();
      setTimeout(() => input.current?.focus(), 0);
    };
    const onClick = (e: MouseEvent) => {
      if ((e.target as Element | null)?.closest?.("[data-waitlist-open]")) {
        e.preventDefault();
        open();
      }
    };
    const onHash = () => window.location.hash === "#waitlist" && open();
    document.addEventListener("click", onClick);
    window.addEventListener("hashchange", onHash);
    onHash();
    return () => {
      document.removeEventListener("click", onClick);
      window.removeEventListener("hashchange", onHash);
    };
  }, []);

  function close() {
    dialog.current?.close();
    if (window.location.hash === "#waitlist") history.replaceState(null, "", window.location.pathname + window.location.search);
  }

  async function submit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setState({ kind: "sending" });
    try {
      const res = await fetch("/api/waitlist", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: form.get("email"),
          company: form.get("company"),
          page: window.location.pathname,
          referrer: document.referrer,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Something went wrong. Please try again.");
      setState({ kind: "done" });
    } catch (err) {
      setState({ kind: "error", message: err instanceof Error ? err.message : "Something went wrong." });
    }
  }

  return (
    <dialog
      ref={dialog}
      className="lp-modal"
      aria-labelledby="waitlist-title"
      onClick={(e) => e.target === dialog.current && close()}
      onClose={() => window.location.hash === "#waitlist" && history.replaceState(null, "", window.location.pathname)}
    >
      <div className="lp-modal-body">
        <button type="button" className="lp-modal-x" aria-label="Close" onClick={close}>×</button>
        {state.kind === "done" ? (
          <div className="lp-modal-done" role="status">
            <div className="lp-check" aria-hidden="true">✓</div>
            <h2 id="waitlist-title">You&apos;re on the list</h2>
            <p>We&apos;ll email you when your early-access spot opens up.</p>
            <button type="button" className="lp-btn lp-btn-primary" onClick={close}>Done</button>
          </div>
        ) : (
          <form onSubmit={submit} noValidate={false}>
            <h2 id="waitlist-title">Get early access</h2>
            <p className="lp-muted">
              Join the waitlist and we&apos;ll invite you as spots open. One email when it&apos;s your turn. No spam.
            </p>
            <label htmlFor="waitlist-email" className="lp-label">Email address</label>
            <input
              ref={input}
              id="waitlist-email"
              name="email"
              type="email"
              inputMode="email"
              autoComplete="email"
              required
              maxLength={254}
              placeholder="you@example.com"
              className="lp-input"
            />
            {/* Honeypot for bots: hidden from people and screen readers. */}
            <div className="lp-hp" aria-hidden="true">
              <label>
                Company
                <input name="company" type="text" tabIndex={-1} autoComplete="off" />
              </label>
            </div>
            <p className="lp-fine">
              {CONSENT_TEXT}{" "}
              <a href="/privacy" target="_blank" rel="noopener">Privacy policy</a>
            </p>
            {state.kind === "error" && <p className="lp-error" role="alert">{state.message}</p>}
            <button type="submit" className="lp-btn lp-btn-primary lp-btn-block" disabled={state.kind === "sending"}>
              {state.kind === "sending" ? "Joining…" : "Join the waitlist"}
            </button>
          </form>
        )}
      </div>
    </dialog>
  );
}
