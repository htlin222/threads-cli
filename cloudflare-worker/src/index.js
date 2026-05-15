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
};
