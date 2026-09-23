import { ImageResponse } from "next/og";
import { BOLT_PATH } from "@/lib/brand";
import { site } from "@/lib/site";

export const alt = `${site.name}: find profitable eBay dropshipping products matched to CJdropshipping`;
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

export default function OpengraphImage() {
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%", height: "100%", display: "flex", flexDirection: "column", justifyContent: "space-between",
          padding: "72px", background: "linear-gradient(135deg, #0f172a 0%, #1e3a8a 100%)", color: "white",
          fontFamily: "sans-serif",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 18, fontSize: 34, fontWeight: 700 }}>
          <div style={{ display: "flex", width: 56, height: 56, borderRadius: 14, background: "#2952e3", alignItems: "center", justifyContent: "center" }}>
            <svg viewBox="0 0 64 64" width="34" height="34"><path d={BOLT_PATH} fill="#ffffff" /></svg>
          </div>
          {site.name}
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
          <div style={{ fontSize: 64, fontWeight: 800, lineHeight: 1.1, letterSpacing: "-0.02em" }}>
            Find profitable eBay dropshipping products
          </div>
          <div style={{ fontSize: 32, color: "#c7d2fe" }}>
            Matched to CJdropshipping with real cost, shipping and profit, then listed in one click.
          </div>
        </div>
        <div style={{ display: "flex", gap: 28, fontSize: 26, color: "#e0e7ff" }}>
          <span>Official eBay APIs</span><span>·</span><span>Real shipping quotes</span><span>·</span><span>Your rules, every time</span>
        </div>
      </div>
    ),
    size,
  );
}
