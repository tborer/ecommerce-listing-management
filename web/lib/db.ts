import { Pool } from "pg";

// Postgres (Neon on Vercel). Connect the same Neon database to this project
// so DATABASE_URL is set here too. One small pool per server instance.

const g = globalThis as unknown as { __ssPool?: Pool | null };

export function databaseUrl(): string | undefined {
  return process.env.DATABASE_URL || process.env.POSTGRES_URL || undefined;
}

export function getPool(): Pool | null {
  if (g.__ssPool !== undefined) return g.__ssPool;
  const url = databaseUrl();
  g.__ssPool = url
    ? new Pool({ connectionString: url, max: 3, idleTimeoutMillis: 10_000, connectionTimeoutMillis: 5_000 })
    : null;
  return g.__ssPool;
}
