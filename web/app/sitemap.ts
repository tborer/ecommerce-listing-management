import type { MetadataRoute } from "next";
import { PRIVACY_UPDATED, site } from "@/lib/site";

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    { url: `${site.url}/`, lastModified: new Date(), changeFrequency: "weekly", priority: 1 },
    { url: `${site.url}/privacy`, lastModified: new Date(PRIVACY_UPDATED), changeFrequency: "yearly", priority: 0.3 },
  ];
}
