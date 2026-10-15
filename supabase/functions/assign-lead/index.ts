// Supabase Edge Function: assign an incoming Respond.io contact to the agent listed in the Google Sheet.
//
// Respond.io Workflow -> HTTP Request (POST, JSON) -> this function:
//   { "contact_id": "...", "phone": "...", "email": "..." }
// Steps: look the tenant up in the sheet -> if no row, stop -> if Respond.io already shows an
// assignee, stop -> otherwise assign the agent from the sheet.
//
// Secrets (Supabase > Edge Functions > Secrets):
//   RESPONDIO_API_TOKEN, SHEET_CSV_URL, WEBHOOK_SECRET   (required)
//   SHEET_PHONE_COL (default "phone"), SHEET_EMAIL_COL (default "email"),
//   SHEET_AGENT_COL (default "assignee")                 (optional: your sheet's header names)

const API = "https://api.respond.io/v2";
const env = (k: string, d = "") => Deno.env.get(k) ?? d;
const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

// Minimal CSV parser (handles quoted fields and commas inside quotes).
function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [], cell = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; }
      else if (c === '"') q = false;
      else cell += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(cell); cell = ""; rows.push(row); row = [];
    } else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

const digits = (s: string) => s.replace(/\D/g, "");
const phoneKey = (s: string) => digits(s).slice(-10); // ignore country code / formatting
const emailKey = (s: string) => s.trim().toLowerCase();

// The sheet changes daily, so re-read it every few minutes instead of on every message.
let cache: { at: number; byPhone: Map<string, string>; byEmail: Map<string, string> } | null = null;
async function loadSheet() {
  if (cache && Date.now() - cache.at < 5 * 60_000) return cache;
  const r = await fetch(env("SHEET_CSV_URL"));
  if (!r.ok) throw new Error(`Sheet fetch failed: ${r.status}`);
  const [head, ...rows] = parseCsv(await r.text());
  const col = (name: string) => head.findIndex((h) => h.trim().toLowerCase() === name.toLowerCase());
  const p = col(env("SHEET_PHONE_COL", "phone"));
  const e = col(env("SHEET_EMAIL_COL", "email"));
  const a = col(env("SHEET_AGENT_COL", "assignee"));
  if (a < 0 || (p < 0 && e < 0)) throw new Error("Sheet headers not found; check SHEET_*_COL secrets");
  const byPhone = new Map<string, string>(), byEmail = new Map<string, string>();
  for (const row of rows) {
    const agent = (row[a] ?? "").trim();
    if (!agent) continue;
    if (p >= 0 && phoneKey(row[p] ?? "").length >= 7) byPhone.set(phoneKey(row[p]), agent);
    if (e >= 0 && (row[e] ?? "").trim()) byEmail.set(emailKey(row[e]), agent);
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
    const sheet = await loadSheet();
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
