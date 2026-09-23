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
  name: process.env.SITE_NAME?.trim() || "Listing Manager",
  url: siteUrl(),
  tagline: "eBay dropshipping software that finds profitable products and matches them to CJdropshipping",
  description:
    "Find profitable eBay dropshipping products automatically. Listing Manager scans eBay deals, matches each item to a CJdropshipping supplier with real cost and shipping, checks your profit rules, and lists winners on eBay with one click.",
};

export const waitlistEnabled = () => truthy(process.env.ENABLE_WAITLIST);
