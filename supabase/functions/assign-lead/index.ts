// Supabase Edge Function: assign an incoming Respond.io contact to the agent listed in the Supabase leads table.
//
// Respond.io Workflow -> HTTP Request (POST, JSON) -> this function:
//   { "contact_id": "...", "phone": "...", "email": "..." }
// Steps: look the tenant up in the leads table -> if no row, stop -> if Respond.io already shows an
// assignee, stop -> otherwise assign the agent from the table.
//
// Secrets (Supabase > Edge Functions > Secrets):
//   RESPONDIO_API_TOKEN, WEBHOOK_SECRET, LEADS_TABLE      (required)
//   LEADS_PHONE_COL (default "phone"), LEADS_EMAIL_COL (default "email"),
//   LEADS_AGENT_COL (default "assignee")                  (optional: your column names)
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are provided to the function automatically.

const API = "https://api.respond.io/v2";
const env = (k: string, d = "") => Deno.env.get(k) ?? d;
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const digits = (s: string) => s.replace(/\D/g, "");
const phoneKey = (s: string) => digits(s).slice(-10); // ignore country code / formatting
const emailKey = (s: string) => s.trim().toLowerCase();

// The leads table changes daily, so re-read it every few minutes instead of on every message.
let cache: { at: number; byPhone: Map<string, string>; byEmail: Map<string, string> } | null = null;
async function loadLeads() {
  if (cache && Date.now() - cache.at < 5 * 60_000) return cache;
  const table = env("LEADS_TABLE");
  const p = env("LEADS_PHONE_COL", "phone"), e = env("LEADS_EMAIL_COL", "email"), a = env("LEADS_AGENT_COL", "assignee");
  const cols = [p, e, a].filter(Boolean).join(",");
  const byPhone = new Map<string, string>(), byEmail = new Map<string, string>();
  for (let from = 0; ; from += 1000) { // PostgREST returns at most 1000 rows per request
    const r = await fetch(`${env("SUPABASE_URL")}/rest/v1/${table}?select=${cols}`, {
      headers: {
        apikey: env("SUPABASE_SERVICE_ROLE_KEY"),
        Authorization: `Bearer ${env("SUPABASE_SERVICE_ROLE_KEY")}`,
        Range: `${from}-${from + 999}`,
      },
    });
    if (!r.ok) throw new Error(`Supabase read of '${table}' failed: ${r.status} ${await r.text()}`);
    const rows: Record<string, unknown>[] = await r.json();
    for (const row of rows) {
      const agent = String(row[a] ?? "").trim();
      if (!agent) continue;
      const ph = String(row[p] ?? ""), em = String(row[e] ?? "");
      if (phoneKey(ph).length >= 7) byPhone.set(phoneKey(ph), agent);
      if (em.trim()) byEmail.set(emailKey(em), agent);
    }
    if (rows.length < 1000) break;
  }
  cache = { at: Date.now(), byPhone, byEmail };
  return cache;
}

const rio = (method: string, path: string, body?: unknown) =>
  fetch(API + path, {
    method,
    headers: { Authorization: `Bearer ${env("RESPONDIO_API_TOKEN")}`, "content-type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });

Deno.serve(async (req) => {
  if (req.method !== "POST") return json({ error: "POST only" }, 405);
  if (req.headers.get("x-webhook-secret") !== env("WEBHOOK_SECRET")) return json({ error: "unauthorized" }, 401);

  const { contact_id, phone = "", email = "" } = await req.json().catch(() => ({}));
  if (!contact_id && !phone && !email) return json({ error: "need contact_id, phone or email" }, 400);

  try {
    const sheet = await loadLeads();
    const agent = (phone && sheet.byPhone.get(phoneKey(phone))) || (email && sheet.byEmail.get(emailKey(email)));
    if (!agent) return json({ status: "no_match" });

    const ident = contact_id ? `id:${contact_id}` : email ? `email:${email}` : `phone:+${digits(phone)}`;
    const c = await rio("GET", `/contact/${ident}`);
    if (!c.ok) return json({ status: "contact_lookup_failed", code: c.status, detail: await c.text() }, 502);
    const existing = (await c.json()).assignee;
    if (existing) return json({ status: "already_assigned", assignee: existing.email ?? existing });

    const a = await rio("POST", `/contact/${ident}/conversation/assignee`, {
      assignee: /^\d+$/.test(agent) ? Number(agent) : agent,
    });
    if (!a.ok) return json({ status: "assign_failed", code: a.status, detail: await a.text() }, 502);
    return json({ status: "assigned", assignee: agent });
  } catch (err) {
    return json({ status: "error", detail: String(err) }, 500);
  }
});
