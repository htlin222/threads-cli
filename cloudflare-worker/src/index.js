// Threads OAuth callback relay.
//
// GET /callback?code=...&state=...  ->  stashes the code in KV keyed by state.
// GET /poll?state=...                ->  returns the code (204 if not yet present),
//                                        deletes it on read (single-use).
//
// The Python script generates a random `state` per run, embeds it in the auth URL,
// then polls /poll?state=<same value> until it gets the code. No shared secret on
// the worker side -- the state itself is the bearer credential, so use >=32 chars.

const HTML_OK = `<!doctype html>
<meta charset=utf-8>
<title>Threads OAuth callback</title>
<style>body{font:16px system-ui;margin:4rem auto;max-width:32rem;padding:0 1rem}</style>
<h2>Threads OAuth code captured.</h2>
<p>You can close this tab &mdash; the script will pick it up automatically.</p>`;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/callback") {
      const code = url.searchParams.get("code");
      const state = url.searchParams.get("state");
      const error = url.searchParams.get("error");
      if (error) {
        return new Response(
          `OAuth error: ${error} - ${url.searchParams.get("error_description") || ""}`,
          { status: 400 }
        );
      }
      if (!code || !state) {
        return new Response("missing code or state", { status: 400 });
      }
      await env.THREADS_AUTH.put(`code:${state}`, code, { expirationTtl: 600 });
      return new Response(HTML_OK, {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }

    if (url.pathname === "/poll") {
      const state = url.searchParams.get("state");
      if (!state) return new Response("missing state", { status: 400 });
      const code = await env.THREADS_AUTH.get(`code:${state}`);
      if (!code) return new Response("", { status: 204 });
      await env.THREADS_AUTH.delete(`code:${state}`);
      return new Response(code, {
        headers: { "content-type": "text/plain" },
      });
    }

    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response("threads-cb worker ok", { status: 200 });
    }

    return new Response("not found", { status: 404 });
  },

  // Cron trigger (see [triggers] crons in wrangler.toml): fetch FEED_URL, post the
  // newest not-yet-seen item to Threads. Token lives in KV and self-refreshes.
  async scheduled(_controller, env, ctx) {
    ctx.waitUntil(runCron(env));
  },
};

// ---------- Scheduled posting ----------

const GRAPH = "https://graph.threads.net";
const SEEN_MAX = 200; // rolling de-dup window

async function runCron(env) {
  try {
    const t = await kvToken(env);
    if (!t.token || !t.userId) {
      console.error("cron: no token/user_id in KV -- run `make worker-seed-token`");
      return;
    }
    const token = await ensureFreshToken(env, t);

    const items = await fetchFeedItems(env);
    if (!items.length) {
      console.log("cron: feed empty, nothing to post");
      return;
    }

    const seen = await loadSeen(env);
    const seenSet = new Set(seen);
    const next = items.find((it) => !seenSet.has(it.key));
    if (!next) {
      console.log("cron: no new item");
      return;
    }

    if (env.DRY_RUN === "1") {
      console.log(`cron DRY_RUN: would post (key=${next.key}): ${next.text}`);
      return; // do NOT touch feed:seen on a dry run
    }

    const res = await postText(env, token, t.userId, next.text);
    console.log(`cron: posted thread ${res.id} (key=${next.key})`);
    await saveSeen(env, seen, next.key);
  } catch (err) {
    console.error(`cron error: ${err && err.stack ? err.stack : err}`);
  }
}

// ---- Token (KV-backed, self-refreshing) ----

async function kvToken(env) {
  const [token, expiresAt, userId] = await Promise.all([
    env.THREADS_AUTH.get("token:access"),
    env.THREADS_AUTH.get("token:expires_at"),
    env.THREADS_AUTH.get("token:user_id"),
  ]);
  return { token, expiresAt: Number(expiresAt) || 0, userId };
}

async function ensureFreshToken(env, t) {
  const now = Math.floor(Date.now() / 1000);
  if (t.expiresAt - now >= 86400) return t.token; // >1 day of runway, leave it
  console.log("cron: token within 1 day of expiry, refreshing");
  const j = await refreshToken(t.token);
  const newToken = j.access_token;
  const expiresIn = Number(j.expires_in) || 0;
  await Promise.all([
    env.THREADS_AUTH.put("token:access", newToken),
    env.THREADS_AUTH.put("token:expires_at", String(now + expiresIn)),
  ]);
  return newToken;
}

async function refreshToken(token) {
  // GET {GRAPH}/refresh_access_token -- no /v1.0, no client secret (mirrors threads_lib.py).
  const url =
    `${GRAPH}/refresh_access_token?grant_type=th_refresh_token` +
    `&access_token=${encodeURIComponent(token)}`;
  const r = await fetch(url);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`refresh failed: ${JSON.stringify(j.error || j)}`);
  return j;
}

// ---- Feed parsing (JSON Feed + lightweight RSS/Atom) ----

async function fetchFeedItems(env) {
  if (!env.FEED_URL) throw new Error("FEED_URL not configured");
  const r = await fetch(env.FEED_URL, {
    headers: { "user-agent": "threads-cb-cron/1.0" },
  });
  if (!r.ok) throw new Error(`feed fetch failed: HTTP ${r.status}`);
  const ctype = (r.headers.get("content-type") || "").toLowerCase();
  const body = await r.text();

  const raw =
    ctype.includes("json") || body.trimStart().startsWith("{")
      ? parseJsonFeed(body)
      : parseXmlFeed(body);

  raw.sort((a, b) => (b.date || 0) - (a.date || 0)); // newest first when dated
  const out = [];
  for (const it of raw) {
    if (!it.title && !it.link) continue;
    out.push({ key: await itemKey(it), text: formatPost(it) });
  }
  return out;
}

function parseJsonFeed(body) {
  const j = JSON.parse(body);
  const items = Array.isArray(j.items) ? j.items : [];
  return items.map((it) => ({
    id: it.id || it.url || "",
    title: it.title || "",
    link: it.url || it.external_url || "",
    date: it.date_published ? Date.parse(it.date_published) || 0 : 0,
  }));
}

function parseXmlFeed(body) {
  const blocks = body.match(/<(item|entry)\b[\s\S]*?<\/\1>/gi) || [];
  return blocks.map((b) => {
    const title = unescapeXml(xmlTag(b, "title"));
    let link = xmlTag(b, "link"); // RSS: <link>url</link>
    if (!link) {
      const m = b.match(/<link\b[^>]*href="([^"]+)"/i); // Atom: <link href="url"/>
      link = m ? m[1] : "";
    }
    const guid = xmlTag(b, "guid") || xmlTag(b, "id") || link;
    const dstr = xmlTag(b, "pubDate") || xmlTag(b, "updated") || xmlTag(b, "published");
    return {
      id: unescapeXml(guid),
      title,
      link: unescapeXml(link),
      date: dstr ? Date.parse(dstr) || 0 : 0,
    };
  });
}

function xmlTag(block, name) {
  const m = block.match(new RegExp(`<${name}\\b[^>]*>([\\s\\S]*?)</${name}>`, "i"));
  if (!m) return "";
  return m[1].replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1").trim();
}

function unescapeXml(s) {
  return s
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&apos;/g, "'")
    .replace(/&amp;/g, "&");
}

function formatPost(it) {
  const MAX = 500; // Threads text limit
  const title = (it.title || "").trim();
  const link = (it.link || "").trim();
  if (!link) return title.slice(0, MAX);
  const suffix = `\n\n${link}`;
  const room = MAX - suffix.length;
  const head =
    title.length > room
      ? title.slice(0, Math.max(0, room - 1)).trimEnd() + "…"
      : title;
  return (head + suffix).slice(0, MAX);
}

// Stable de-dup key: feed-provided guid/id when present, else SHA-256(link+title).
async function itemKey(it) {
  if (it.id) return it.id;
  const basis = `${it.link} ${it.title}`;
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(basis));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ---- Seen-set (rolling, most-recent-first) ----

async function loadSeen(env) {
  const raw = await env.THREADS_AUTH.get("feed:seen");
  if (!raw) return [];
  try {
    const a = JSON.parse(raw);
    return Array.isArray(a) ? a : [];
  } catch {
    return [];
  }
}

async function saveSeen(env, seen, key) {
  const next = [key, ...seen.filter((k) => k !== key)].slice(0, SEEN_MAX);
  await env.THREADS_AUTH.put("feed:seen", JSON.stringify(next));
}

// ---- Threads publish (two-step create -> publish; mirrors threads_lib.gpost) ----

async function postText(env, token, userId, text) {
  let tok = token;
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const container = await gpost(`/${userId}/threads`, tok, {
        media_type: "TEXT",
        text,
      });
      return await gpost(`/${userId}/threads_publish`, tok, {
        creation_id: container.id,
      });
    } catch (err) {
      if (err.code === 190 && attempt === 0) {
        console.log("cron: 190 auth error, refreshing token and retrying");
        const t = await kvToken(env);
        tok = await ensureFreshToken(env, { ...t, expiresAt: 0 }); // force refresh
        continue;
      }
      throw err;
    }
  }
}

async function gpost(path, token, params) {
  const body = new URLSearchParams({ ...params, access_token: token });
  const r = await fetch(`${GRAPH}/v1.0${path}`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(`POST ${path} failed: ${JSON.stringify(j.error || j)}`);
    e.code = j.error && j.error.code;
    throw e;
  }
  return j;
}
