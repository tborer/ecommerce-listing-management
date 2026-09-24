import type { MetadataRoute } from "next";
import { LANDING_UPDATED, PRIVACY_UPDATED, site } from "@/lib/site";

// Public, indexable pages only. Add every new public page here (and nothing
// under /dashboard, /settings, /connections, /login or /api -- see robots.ts).
// URLs use SITE_URL, or Vercel's production domain when that isn't set.
export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: `${site.url}/`, lastModified: new Date(LANDING_UPDATED), changeFrequency: "weekly", priority: 1 },
    { url: `${site.url}/privacy`, lastModified: new Date(PRIVACY_UPDATED), changeFrequency: "yearly", priority: 0.3 },
  ];
}
