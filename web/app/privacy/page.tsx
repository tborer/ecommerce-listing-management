import type { Metadata } from "next";
import Link from "next/link";
import SiteFooter from "@/components/SiteFooter";
import { BOLT_PATH, CONSENT_TEXT } from "@/lib/brand";
import { contactEmail, operatorName, PRIVACY_UPDATED, site } from "@/lib/site";
import "../landing.css";

export const metadata: Metadata = {
  title: "Privacy Policy",
  description: `How ${site.name} collects, uses and protects your information, including waitlist emails, account data and connected eBay and CJdropshipping accounts.`,
  alternates: { canonical: "/privacy" },
};

function Contact() {
  const email = contactEmail();
  return email ? (
    <a href={`mailto:${email}`}>{email}</a>
  ) : (
    <>reply to any email you&apos;ve received from us</>
  );
}

export default function PrivacyPage() {
  const name = site.name;
  const operator = operatorName();
  const updated = new Date(`${PRIVACY_UPDATED}T12:00:00Z`).toLocaleDateString("en-US", { dateStyle: "long", timeZone: "UTC" });
  return (
    <div className="lp">
      <header className="lp-header">
        <div className="lp-wrap lp-header-inner">
          <Link href="/" className="lp-logo" aria-label={`${name} home`}>
            <span className="lp-logo-mark" aria-hidden="true">
              <svg viewBox="0 0 64 64" width="18" height="18"><path d={BOLT_PATH} fill="currentColor" /></svg>
            </span>
            <span className="lp-logo-text">{name}</span>
          </Link>
          <span className="lp-nav" />
          <Link href="/" className="lp-link">← Back to home</Link>
        </div>
      </header>

      <main id="main" className="lp-wrap lp-legal">
        <h1>Privacy Policy</h1>
        <p className="lp-updated">Last updated: {updated}</p>

        <p>
          This policy explains what information {name}
          {operator ? <> (operated by {operator})</> : null} collects, how we use it, and the choices you have.
          {" "}{name} helps sellers find eBay products, match them to CJdropshipping suppliers, and create listings
          on their own eBay accounts. We keep what we collect to what the service needs, and we don&apos;t sell
          your personal information.
        </p>

        <h2>Information we collect</h2>
        <h3>When you join the waitlist</h3>
        <ul>
          <li>Your <strong>email address</strong>.</li>
          <li>When you signed up, the page you signed up from, the page that referred you (if your browser sends it),
            and your browser&apos;s user-agent string.</li>
          <li>The statement you agreed to when signing up: &ldquo;{CONSENT_TEXT}&rdquo;</li>
        </ul>

        <h3>When you create an account</h3>
        <ul>
          <li>Your <strong>email address</strong> and <strong>password</strong>. We store only a one-way hash of your
            password, never the password itself.</li>
          <li>A <strong>session cookie</strong> that keeps you logged in (see Cookies below).</li>
          <li>The <strong>settings</strong> you choose: search categories and keywords, profit and shipping
            criteria, schedule, listing preferences, and the item location you enter.</li>
        </ul>

        <h3>When you connect other services</h3>
        <ul>
          <li><strong>CJdropshipping:</strong> the API key you provide and the access tokens CJ issues for it.</li>
          <li><strong>eBay:</strong> the authorization tokens eBay issues when you approve access. We never see your
            eBay password.</li>
          <li><strong>Data from those services:</strong> public eBay listing information found by your searches;
            CJ product, price, stock and shipping information; your eBay business policies; and details of the
            listings you create through {name}.</li>
        </ul>

        <h3>Technical information</h3>
        <p>
          Our hosting provider records standard server logs (such as IP address, request time and pages requested)
          to operate and secure the service. We don&apos;t use these logs to build profiles of you.
        </p>

        <h2>How we use information</h2>
        <ul>
          <li>To run the service you asked for: searching eBay, matching CJ products, checking your criteria, and
            creating listings on your eBay account when you (or your auto-list setting) ask us to.</li>
          <li>To contact waitlist members about early access, and account holders about their account and the service.</li>
          <li>To keep the service secure, prevent abuse and fix problems.</li>
        </ul>
        <p>We don&apos;t sell your personal information, and we don&apos;t use it for third-party advertising.</p>

        <h2>Cookies</h2>
        <p>
          We use a single essential cookie, set when you log in, to keep your session. We don&apos;t use advertising
          or tracking cookies, and the public pages set no cookies. If that changes, we&apos;ll update this policy
          first.
        </p>

        <h2>Who we share information with</h2>
        <p>Only with providers that help us run {name}, and only what they need:</p>
        <ul>
          <li><strong>Vercel</strong>, which hosts the website and application.</li>
          <li><strong>Neon</strong>, which hosts our database.</li>
          <li><strong>Our email provider</strong>, which delivers our emails.</li>
          <li><strong>eBay</strong> and <strong>CJdropshipping</strong>, but only when you connect them, and only to do
            what you ask (for example, creating a listing on your eBay account).</li>
        </ul>
        <p>
          We may also disclose information if the law requires it, to protect the rights and safety of our users or
          others, or as part of a merger or sale of the service. In a sale, this policy would continue to apply to
          your information.
        </p>

        <h2>How we protect information</h2>
        <ul>
          <li>All traffic to {name} uses HTTPS.</li>
          <li>CJdropshipping API keys and eBay tokens are encrypted before they&apos;re stored, and they&apos;re never
            shown again after you save them.</li>
          <li>Passwords are stored only as one-way hashes.</li>
        </ul>
        <p>No system is perfectly secure, but we work to protect your information and limit who can access it.</p>

        <h2>How long we keep it</h2>
        <ul>
          <li><strong>Waitlist emails:</strong> until you ask us to remove yours, or until we no longer need the
            waitlist.</li>
          <li><strong>Account data:</strong> for as long as your account is open. When you disconnect CJdropshipping or
            eBay, we delete the stored key or tokens right away.</li>
        </ul>

        <h2>Your choices and rights</h2>
        <p>
          You can ask to access, correct, or delete your personal information, or to be removed from the waitlist,
          by contacting us (see below). You can disconnect eBay or CJdropshipping at any time from the Connections
          page. Depending on where you live (for example, the EU, UK or California), you may have additional
          rights. We&apos;ll honor them, and we won&apos;t treat you differently for using them.
        </p>

        <h2>Children</h2>
        <p>{name} is a business tool for adults and isn&apos;t directed to anyone under 18. We don&apos;t knowingly
          collect information from children.</p>

        <h2>Where information is processed</h2>
        <p>Our service providers may process and store information in the United States and other countries. When
          information is transferred, we rely on the safeguards those providers offer.</p>

        <h2>Changes to this policy</h2>
        <p>If we change this policy, we&apos;ll update the date at the top of this page. If a change is significant,
          we&apos;ll also tell you by email or in the app.</p>

        <h2>Contact us</h2>
        <p>For privacy questions or requests, <Contact />.</p>
      </main>

      <SiteFooter onHome={false} />
    </div>
  );
}
