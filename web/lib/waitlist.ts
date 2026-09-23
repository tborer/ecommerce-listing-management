import nodemailer from "nodemailer";
import { getPool } from "@/lib/db";
import { BRAND, CONSENT_TEXT } from "@/lib/brand";

// Waitlist sign-ups are emailed to the site owner through their SMTP server.
// The owner's address never reaches the browser: it's only used here, server-side.

const EMAIL_RE = /^[^\s@<>()[\]\\,;:"]+@[^\s@<>()[\]\\,;:"]+\.[^\s@<>()[\]\\,;:"]{2,}$/;

export function normalizeEmail(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const email = raw.trim().toLowerCase();
  if (email.length > 254 || /[\r\n]/.test(email) || !EMAIL_RE.test(email)) return null;
  return email;
}

const truthy = (v: string | undefined) => ["1", "true", "yes", "on"].includes((v ?? "").trim().toLowerCase());

export type SmtpConfig = {
  host: string;
  port: number;
  secure: boolean;
  user?: string;
  pass?: string;
  from: string;
  to: string;
};

export function smtpConfig(env: NodeJS.ProcessEnv = process.env): SmtpConfig | null {
  const host = env.SMTP_HOST?.trim();
  const from = env.SMTP_FROM?.trim() || env.SMTP_USER?.trim();
  // Where sign-ups are delivered: WAITLIST_TO if set, otherwise the SMTP account itself.
  const to = env.WAITLIST_TO?.trim() || env.SMTP_USER?.trim() || from;
  if (!host || !from || !to) return null;
  const secure = truthy(env.SMTP_SECURE);
  return {
    host,
    port: Number(env.SMTP_PORT) || (secure ? 465 : 587),
    secure,
    user: env.SMTP_USER?.trim() || undefined,
    pass: env.SMTP_PASS || undefined,
    from,
    to,
  };
}

export async function sendWaitlistEmail(
  cfg: SmtpConfig,
  signup: { email: string; page?: string; referrer?: string; userAgent?: string },
): Promise<void> {
  const transport = nodemailer.createTransport({
    host: cfg.host,
    port: cfg.port,
    secure: cfg.secure, // true = TLS from the start (usually 465); false = STARTTLS when offered (587)
    auth: cfg.user ? { user: cfg.user, pass: cfg.pass } : undefined,
    connectionTimeout: 10_000,
    greetingTimeout: 10_000,
    socketTimeout: 15_000,
  });
  const when = new Date().toISOString();
  await transport.sendMail({
    from: cfg.from,
    to: cfg.to,
    replyTo: signup.email,
    subject: `${BRAND} waitlist sign-up: ${signup.email}`,
    text: [
      `New waitlist sign-up`,
      ``,
      `Email:    ${signup.email}`,
      `Time:     ${when}`,
      `Page:     ${signup.page ?? "-"}`,
      `Referrer: ${signup.referrer || "-"}`,
      `Browser:  ${signup.userAgent ?? "-"}`,
    ].join("\n"),
  });
}

// Best-effort abuse brake, per server instance (serverless instances don't
// share memory, so this only slows down a single noisy client).
const WINDOW_MS = 10 * 60 * 1000;
const MAX_PER_WINDOW = 5;
const hits = new Map<string, number[]>();

export function rateLimited(key: string, now = Date.now()): boolean {
  const recent = (hits.get(key) ?? []).filter((t) => now - t < WINDOW_MS);
  recent.push(now);
  hits.set(key, recent);
  if (hits.size > 5000) hits.clear();
  return recent.length > MAX_PER_WINDOW;
}


let schemaReady: Promise<void> | null = null;

function ensureSchema(): Promise<void> {
  const pool = getPool();
  if (!pool) return Promise.resolve();
  schemaReady ??= pool
    .query(`
      CREATE TABLE IF NOT EXISTS waitlist_signups (
        id           BIGSERIAL PRIMARY KEY,
        email        TEXT NOT NULL UNIQUE,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        status       TEXT NOT NULL DEFAULT 'waiting',
        page         TEXT,
        referrer     TEXT,
        user_agent   TEXT,
        consent_text TEXT,
        notified_at  TIMESTAMPTZ
      )`)
    .then(() => undefined)
    .catch((err) => {
      schemaReady = null; // retry on the next request
      throw err;
    });
  return schemaReady;
}

export type SaveResult = "saved" | "duplicate" | "no-database";

/** Stores a sign-up. "duplicate" means the email was already on the list. */
export async function saveSignup(signup: {
  email: string; page?: string; referrer?: string; userAgent?: string;
}): Promise<{ result: SaveResult; id?: string }> {
  const pool = getPool();
  if (!pool) return { result: "no-database" };
  await ensureSchema();
  const res = await pool.query<{ id: string }>(
    `INSERT INTO waitlist_signups (email, page, referrer, user_agent, consent_text)
     VALUES ($1, $2, $3, $4, $5)
     ON CONFLICT (email) DO NOTHING
     RETURNING id`,
    [signup.email, signup.page ?? null, signup.referrer || null, signup.userAgent ?? null, CONSENT_TEXT],
  );
  return res.rowCount ? { result: "saved", id: res.rows[0].id } : { result: "duplicate" };
}

export async function markNotified(id: string): Promise<void> {
  await getPool()?.query("UPDATE waitlist_signups SET notified_at = now() WHERE id = $1", [id]);
}
