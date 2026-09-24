import Link from "next/link";
import { apiConfigured, site } from "@/lib/site";

export default function SiteFooter({ onHome = true }: { onHome?: boolean }) {
  const anchor = (id: string) => (onHome ? `#${id}` : `/#${id}`);
  return (
    <footer className="lp-footer">
      <div className="lp-wrap lp-footer-inner">
        <span>© {new Date().getFullYear()} {site.name}</span>
        <nav aria-label="Footer">
          <a href={anchor("how-it-works")}>How it works</a>
          <a href={anchor("faq")}>FAQ</a>
          <Link href="/privacy">Privacy</Link>
          {apiConfigured() && <Link href="/login">Log in</Link>}
        </nav>
        <span className="lp-muted small">Not affiliated with eBay Inc. or CJdropshipping.</span>
      </div>
    </footer>
  );
}
