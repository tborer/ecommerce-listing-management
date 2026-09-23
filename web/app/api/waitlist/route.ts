import { NextResponse } from "next/server";
import { waitlistEnabled } from "@/lib/site";
import {
  markNotified,
  normalizeEmail,
  rateLimited,
  saveSignup,
  sendWaitlistEmail,
  smtpConfig,
  type SaveResult,
} from "@/lib/waitlist";

// Handled here by Next.js (filesystem routes win over the /api/* rewrite to the Python API).
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const str = (v: unknown, max = 300) => (typeof v === "string" ? v.slice(0, max) : undefined);

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

  const signup = {
    email,
    page: str(body.page),
    referrer: str(body.referrer),
    userAgent: req.headers.get("user-agent")?.slice(0, 300) ?? undefined,
  };

  // 1) Save it. A database problem must not lose the sign-up: fall through to email.
  let saved: { result: SaveResult | "error"; id?: string };
  try {
    saved = await saveSignup(signup);
  } catch (err) {
    console.error("waitlist: saving to the database failed", err);
    saved = { result: "error" };
  }
  // Already on the list: same friendly answer, no second notification.
  if (saved.result === "duplicate") return NextResponse.json({ ok: true });
  const stored = saved.result === "saved";

  // 2) Tell the owner.
  const cfg = smtpConfig();
  if (!cfg) {
    if (stored) return NextResponse.json({ ok: true });
    console.error("waitlist: no database and no SMTP configured -- sign-up could not be recorded");
    return NextResponse.json({ error: "Sign-ups are temporarily unavailable. Please try again later." }, { status: 503 });
  }
  try {
    await sendWaitlistEmail(cfg, signup);
    if (stored && saved.id) await markNotified(saved.id).catch((err) => console.error("waitlist: markNotified", err));
  } catch (err) {
    console.error("waitlist: sending the notification failed", err);
    if (!stored) {
      return NextResponse.json({ error: "We couldn't save your spot just now. Please try again." }, { status: 502 });
    }
  }
  return NextResponse.json({ ok: true });
}
