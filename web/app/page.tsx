import type { Metadata } from "next";
import Link from "next/link";
import WaitlistModal from "@/components/WaitlistModal";
import { site, waitlistEnabled } from "@/lib/site";
import "./landing.css";

export const metadata: Metadata = {
  alternates: { canonical: "/" },
};

const STEPS = [
  {
    title: "Tell it what to look for",
    body: "Pick eBay deal categories or add your own keywords, then set a price range. Nothing to install and no spreadsheets.",
  },
  {
    title: "It scans eBay with official APIs",
    body: "Discovery uses eBay's own Deal and Browse APIs, not page scraping, so your seller account never sits behind a fragile bot.",
  },
  {
    title: "Every item is matched to CJdropshipping",
    body: "Each eBay item is matched to a CJdropshipping product. You see the real variant cost, a live shipping quote, and delivery time.",
  },
  {
    title: "You approve, or it lists the winners",
    body: "Review a clean table and list with one click, or switch on auto-list for items that pass every one of your rules.",
  },
];

const FEATURES = [
  {
    title: "Profit math you can trust",
    body: "Every candidate is checked against your eBay fee rate, target margin, minimum profit, and the actual CJ shipping quote before it's shown as a winner.",
  },
  {
    title: "Match confidence, not guesswork",
    body: "Titles are scored for real product matches. It catches accessories posing as the product, voltage or size mismatches, and brand-name items a generic supplier can't legitimately fill.",
  },
  {
    title: "A review queue that saves hours",
    body: "The eBay item and its supplier match side by side, with cost, shipping, profit, and a pass/fail line for each rule. List, dismiss, or restore in one click.",
  },
  {
    title: "Auto-list with guardrails",
    body: "Off by default. When you turn it on, it lists only items that pass every rule, most profitable first, capped at the number per run that you choose.",
  },
  {
    title: "Runs on a schedule",
    body: "Set a daily time and new candidates are waiting when you log in. Or hit Run now whenever you want fresh results.",
  },
  {
    title: "Your keys stay yours",
    body: "Your CJdropshipping API key and eBay authorization are encrypted before they're stored and never shown again. Listings are created on your own eBay account.",
  },
];

const FAQ = [
  {
    q: "Is dropshipping allowed on eBay?",
    a: "Yes, as long as you fulfill orders from a wholesale supplier. eBay prohibits buying from another retailer or marketplace (like Amazon or Walmart) and shipping it to your buyer. Listing Manager only sources from wholesale dropshipping suppliers such as CJdropshipping, which fits that policy.",
  },
  {
    q: "Which suppliers does it work with?",
    a: "CJdropshipping today, through CJ's official API, using your own CJ account and API key. More suppliers with API-based ordering (including AliExpress's dropshipper program and US-warehouse suppliers) are on the roadmap.",
  },
  {
    q: "Does it create eBay listings automatically?",
    a: "Only if you want it to. By default you review each match and click List on eBay. You can turn on auto-list with a cap per run, and it will only list items that pass every rule you've set.",
  },
  {
    q: "How does it decide an item is profitable?",
    a: "It takes the eBay price, subtracts your fee estimate, the CJ variant cost, and the cheapest CJ shipping option that meets your delivery-time limit. Then it checks the result against your target margin and minimum profit. You can see the math behind every decision.",
  },
  {
    q: "Does it place supplier orders for me?",
    a: "Not yet. Order handoff to CJdropshipping, tracking sync, and marking eBay orders shipped are next on the roadmap. Placing a supplier order will always stay under your control.",
  },
  {
    q: "Is it scraping eBay?",
    a: "No. Product discovery uses eBay's official Deal, Browse, and Taxonomy APIs, and listing uses eBay's Inventory API with your own authorization.",
  },
];

const ACCESS_FAQ = {
  waitlist: {
    q: "When can I get access?",
    a: "We're onboarding sellers in small groups. Join the waitlist and we'll email you when your spot opens.",
  },
  open: {
    q: "How do I get started?",
    a: "Create an account, add your CJdropshipping API key, connect your eBay account, and set your rules. Your first run can start right away.",
  },
};

function Cta({ waitlist, children, variant = "primary" }: { waitlist: boolean; children?: React.ReactNode; variant?: "primary" | "ghost" }) {
  const cls = `lp-btn ${variant === "primary" ? "lp-btn-primary" : "lp-btn-ghost"}`;
  return waitlist ? (
    <a href="#waitlist" data-waitlist-open className={cls}>{children ?? "Join the waitlist"}</a>
  ) : (
    <Link href="/login" className={cls}>{children ?? "Get started"}</Link>
  );
}

// Illustrative product preview (static HTML, not a screenshot) so it stays
// crisp, light, and readable by search engines.
function Preview() {
  const rows = [
    { item: "3-Level Cat Tree with Sisal Posts", ebay: "59.99", cost: "16.40", ship: "7.20", days: "≤12d", profit: "26.19", ok: true },
    { item: "Automatic Pet Water Fountain 2.5L", ebay: "34.99", cost: "9.80", ship: "6.30", days: "≤10d", profit: "12.94", ok: true },
    { item: "Silicone Kitchen Utensil Set, 12 pc", ebay: "29.99", cost: "8.60", ship: "5.90", days: "≤9d", profit: "10.39", ok: true },
    { item: "Stainless Dog Bowl Set, 2 pack", ebay: "24.99", cost: "17.90", ship: "5.10", days: "≤14d", profit: "-2.26", ok: false },
  ];
  return (
    <figure className="lp-preview" aria-label="Example of the review table">
      <div className="lp-preview-bar" aria-hidden="true"><span /><span /><span /></div>
      <div className="lp-preview-head">
        <strong>To review</strong>
        <span className="lp-pill">Run finished · 25 checked · 3 winners</span>
      </div>
      <table className="lp-preview-table">
        <thead>
          <tr><th scope="col">eBay item → CJ match</th><th scope="col">eBay</th><th scope="col">CJ cost</th><th scope="col">Ship</th><th scope="col">Profit</th><th scope="col"><span className="lp-sr">Action</span></th></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.item}>
              <td>{r.item}<span className="lp-sub">{r.ok ? "All rules pass" : "Fails target margin"}</span></td>
              <td>${r.ebay}</td>
              <td>${r.cost}</td>
              <td>${r.ship}<span className="lp-sub">{r.days}</span></td>
              <td className={r.ok ? "lp-pos" : "lp-neg"}>{r.profit.startsWith("-") ? `-$${r.profit.slice(1)}` : `$${r.profit}`}</td>
              <td>{r.ok ? <span className="lp-fake-btn">List</span> : <span className="lp-tag">Skip</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <figcaption>Illustrative example of the review table.</figcaption>
    </figure>
  );
}

export default function LandingPage() {
  const waitlist = waitlistEnabled();
  const faq = [...FAQ, waitlist ? ACCESS_FAQ.waitlist : ACCESS_FAQ.open];
  const jsonLd = [
    {
      "@context": "https://schema.org",
      "@type": "SoftwareApplication",
      name: site.name,
      applicationCategory: "BusinessApplication",
      operatingSystem: "Web",
      url: site.url,
      description: site.description,
      featureList: FEATURES.map((f) => f.title),
    },
    {
      "@context": "https://schema.org",
      "@type": "WebSite",
      name: site.name,
      url: site.url,
    },
    {
      "@context": "https://schema.org",
      "@type": "FAQPage",
      mainEntity: faq.map((f) => ({ "@type": "Question", name: f.q, acceptedAnswer: { "@type": "Answer", text: f.a } })),
    },
  ];

  return (
    <div className="lp">
      <a className="lp-skip" href="#main">Skip to content</a>
      <header className="lp-header">
        <div className="lp-wrap lp-header-inner">
          <Link href="/" className="lp-logo" aria-label={`${site.name} home`}>
            <span className="lp-logo-mark" aria-hidden="true">L</span>
            <span className="lp-logo-text">{site.name}</span>
          </Link>
          <nav className="lp-nav" aria-label="Primary">
            <a href="#how-it-works">How it works</a>
            <a href="#features">Features</a>
            <a href="#faq">FAQ</a>
          </nav>
          <div className="lp-header-cta">
            <Link href="/login" className="lp-link">Log in</Link>
            <Cta waitlist={waitlist} />
          </div>
        </div>
      </header>

      <main id="main">
        <section className="lp-hero">
          <div className="lp-wrap lp-hero-grid">
            <div>
              <p className="lp-eyebrow">eBay dropshipping software · CJdropshipping integration</p>
              <h1>Find profitable eBay dropshipping products, matched to a supplier automatically</h1>
              <p className="lp-lede">
                {site.name} scans eBay deals, matches each item to a CJdropshipping product with the real cost and
                shipping, checks it against your profit rules, and lists the winners on your eBay account in one click.
              </p>
              <div className="lp-cta-row">
                <Cta waitlist={waitlist}>{waitlist ? "Join the waitlist" : "Get started free"}</Cta>
                <a href="#how-it-works" className="lp-btn lp-btn-ghost">See how it works</a>
              </div>
              <ul className="lp-proof" aria-label="Highlights">
                <li>Official eBay APIs, no scraping</li>
                <li>Real CJ shipping quotes</li>
                <li>You stay in control of listings</li>
              </ul>
            </div>
            <Preview />
          </div>
        </section>

        <section className="lp-band" aria-labelledby="problem-title">
          <div className="lp-wrap lp-problem">
            <h2 id="problem-title">Product research shouldn&apos;t take all day</h2>
            <p>
              Finding an eBay item worth selling, hunting for the same product at a supplier, working out fees and
              shipping, then retyping it all into a listing takes hours per product. {site.name} does the searching,
              matching and math for you, so you only spend time on the decisions.
            </p>
          </div>
        </section>

        <section id="how-it-works" className="lp-section" aria-labelledby="how-title">
          <div className="lp-wrap">
            <p className="lp-eyebrow">How it works</p>
            <h2 id="how-title">From eBay deal to live listing in four steps</h2>
            <ol className="lp-steps">
              {STEPS.map((s, i) => (
                <li key={s.title}>
                  <span className="lp-step-num" aria-hidden="true">{i + 1}</span>
                  <h3>{s.title}</h3>
                  <p>{s.body}</p>
                </li>
              ))}
            </ol>
          </div>
        </section>

        <section id="features" className="lp-section lp-section-alt" aria-labelledby="features-title">
          <div className="lp-wrap">
            <p className="lp-eyebrow">Features</p>
            <h2 id="features-title">Built for sellers who care about margin</h2>
            <div className="lp-features">
              {FEATURES.map((f) => (
                <article key={f.title} className="lp-card">
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </article>
              ))}
            </div>
          </div>
        </section>

        <section className="lp-section" aria-labelledby="rules-title">
          <div className="lp-wrap lp-split">
            <div>
              <p className="lp-eyebrow">Your rules, applied every time</p>
              <h2 id="rules-title">Set the criteria once. Every candidate is held to them.</h2>
              <p className="lp-muted-p">
                No more gut-feel listings. Each item shows exactly which rules it passed or failed, so you can tune your
                criteria with confidence.
              </p>
            </div>
            <ul className="lp-rules" aria-label="Example criteria">
              <li><span className="lp-ok" aria-hidden="true">✓</span> eBay price between <b>$10</b> and <b>$100</b></li>
              <li><span className="lp-ok" aria-hidden="true">✓</span> Target margin <b>16%</b> after <b>17%</b> eBay fees</li>
              <li><span className="lp-ok" aria-hidden="true">✓</span> Minimum profit <b>$3.00</b> per sale</li>
              <li><span className="lp-ok" aria-hidden="true">✓</span> Shipping under <b>$15</b>, delivered in <b>15 days</b> or less</li>
              <li><span className="lp-ok" aria-hidden="true">✓</span> Match confidence score of <b>50</b> or more</li>
              <li><span className="lp-ok" aria-hidden="true">✓</span> Optional: US-warehouse products only</li>
            </ul>
          </div>
        </section>

        <section id="faq" className="lp-section lp-section-alt" aria-labelledby="faq-title">
          <div className="lp-wrap lp-faq-wrap">
            <p className="lp-eyebrow">FAQ</p>
            <h2 id="faq-title">Questions sellers ask</h2>
            <div className="lp-faq">
              {faq.map((f) => (
                <details key={f.q}>
                  <summary><h3>{f.q}</h3></summary>
                  <p>{f.a}</p>
                </details>
              ))}
            </div>
          </div>
        </section>

        <section className="lp-final" aria-labelledby="final-title">
          <div className="lp-wrap">
            <h2 id="final-title">Spend your time on decisions, not research</h2>
            <p>
              {waitlist
                ? "We're opening access in small groups. Save your spot and we'll email you when it's your turn."
                : "Connect CJdropshipping and eBay, set your rules, and get your first matches today."}
            </p>
            <Cta waitlist={waitlist}>{waitlist ? "Join the waitlist" : "Get started"}</Cta>
          </div>
        </section>
      </main>

      <footer className="lp-footer">
        <div className="lp-wrap lp-footer-inner">
          <span>© {new Date().getFullYear()} {site.name}</span>
          <nav aria-label="Footer">
            <a href="#how-it-works">How it works</a>
            <a href="#faq">FAQ</a>
            <Link href="/login">Log in</Link>
          </nav>
          <span className="lp-muted small">Not affiliated with eBay Inc. or CJdropshipping.</span>
        </div>
      </footer>

      {waitlist && <WaitlistModal />}
      <script type="application/ld+json" dangerouslySetInnerHTML={{ __html: JSON.stringify(jsonLd).replace(/</g, "\\u003c") }} />
    </div>
  );
}
