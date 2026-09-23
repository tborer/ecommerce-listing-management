import { NextResponse } from "next/server";
import { waitlistEnabled } from "@/lib/site";
import { normalizeEmail, rateLimited, sendWaitlistEmail, smtpConfig } from "@/lib/waitlist";

// Handled here by Next.js (filesystem routes win over the /api/* rewrite to the Python API).
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: Request) {
  if (!waitlistEnabled()) return NextResponse.json({ error: "The waitlist is closed." }, { status: 404 });

  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "Invalid request." }, { status: 400 });
  }

  // Honeypot: real visitors never see or fill the "company" field.
  if (typeof body.company === "string" && body.company.trim()) return NextResponse.json({ ok: true });

  const email = normalizeEmail(body.email);
  if (!email) return NextResponse.json({ error: "Please enter a valid email address." }, { status: 400 });

  const ip = req.headers.get("x-forwarded-for")?.split(",")[0]?.trim() || "unknown";
  if (rateLimited(ip)) {
    return NextResponse.json({ error: "Too many attempts. Please try again in a few minutes." }, { status: 429 });
  }

  const cfg = smtpConfig();
  if (!cfg) {
    console.error("waitlist: SMTP is not configured (need SMTP_HOST and SMTP_FROM or SMTP_USER)");
    return NextResponse.json({ error: "Sign-ups are temporarily unavailable. Please try again later." }, { status: 503 });
  }

  try {
    await sendWaitlistEmail(cfg, {
      email,
      page: typeof body.page === "string" ? body.page.slice(0, 300) : undefined,
      referrer: typeof body.referrer === "string" ? body.referrer.slice(0, 300) : undefined,
      userAgent: req.headers.get("user-agent")?.slice(0, 300) ?? undefined,
    });
  } catch (err) {
    console.error("waitlist: sending failed", err);
    return NextResponse.json({ error: "We couldn't save your spot just now. Please try again." }, { status: 502 });
  }
  return NextResponse.json({ ok: true });
}
