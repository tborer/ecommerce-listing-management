import { BRAND } from "@/lib/brand";

// Site-wide settings for the public pages. Server-only: env vars here are
// read at build time for the static landing page, so changing them in
// Vercel needs a redeploy (Vercel applies env changes to new deployments).

const truthy = (v: string | undefined) => ["1", "true", "yes", "on"].includes((v ?? "").trim().toLowerCase());

function siteUrl(): string {
  const explicit = process.env.SITE_URL?.trim();
  if (explicit) return explicit.replace(/\/$/, "");
  const vercel = process.env.VERCEL_PROJECT_PRODUCTION_URL;
  if (vercel) return `https://${vercel}`;
  return "http://localhost:3000";
}

export const site = {
  name: process.env.SITE_NAME?.trim() || BRAND,
  url: siteUrl(),
  tagline: "eBay dropshipping software that finds profitable products and matches them to CJdropshipping",
  description:
    `Find profitable eBay dropshipping products automatically. ${process.env.SITE_NAME?.trim() || BRAND} scans eBay deals, matches each item to a CJdropshipping supplier with real cost and shipping, checks your profit rules, and lists winners on eBay with one click.`,
};

export const waitlistEnabled = () => truthy(process.env.ENABLE_WAITLIST);

// Privacy policy: bump this date whenever /privacy changes.
export const PRIVACY_UPDATED = "2026-09-23";

// Where people send privacy requests (access, deletion). Shown on /privacy.
// Use a dedicated address (e.g. privacy@yourdomain.com) if you'd rather not publish a personal one.
export const contactEmail = () => process.env.CONTACT_EMAIL?.trim() || null;

// Optional legal name of whoever operates the service, shown on /privacy.
export const operatorName = () => process.env.LEGAL_ENTITY?.trim() || null;

// Whether the API project is connected (API_ORIGIN). Until it is, the public
// site hides "Log in": accounts and the dashboard live in the API.
export const apiConfigured = () => Boolean(process.env.API_ORIGIN?.trim()) || !process.env.VERCEL;
