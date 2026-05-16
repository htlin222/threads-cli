# Threads API + Cloudflare Worker: zero-to-posting tutorial

This is the long version of "how this repo got built" with every gotcha that
cost time, written so a future me (or you) doesn't repeat them.

The end state is: **`make post MSG="hello"` posts a thread.** Everything below
is what it takes to get there the first time.

---

## Why the Cloudflare Worker?

The Threads OAuth flow needs a `redirect_uri` that:

- is HTTPS,
- is registered in your Meta app's Threads API settings (must match **exactly**),
- can receive `?code=...` from Meta's redirect.

Common options:

| Approach                                         | Pros                            | Cons                                                  |
| ------------------------------------------------ | ------------------------------- | ----------------------------------------------------- |
| `https://localhost/`                             | zero infra                      | most browsers block / warn; not really HTTPS          |
| `cloudflared` quick tunnel                       | free, no account                | subdomain changes each run -> re-register every time  |
| `cloudflared` **named** tunnel                   | stable URL                      | requires owning a domain + DNS setup                  |
| **Cloudflare Worker** ✅ (what this repo uses)   | stable URL, no local server, ~free | one-time `wrangler` deploy                          |

The Worker is ~40 lines of JS and gets a stable `https://<name>.<account>.workers.dev`
that we register once with Meta and forget about.

---

## Step-by-step

### 1. Create a Meta App

1. Go to <https://developers.facebook.com/apps/>, **Create App**.
2. Use case: **Other → Business**.
3. After creation, in the left sidebar pick **Use Cases → Add Use Case → Access the Threads API**.

### 2. Add yourself as a Threads tester

1. **Use Cases → Customize → Access the Threads API → Settings**.
2. Scroll down to **用戶權杖產生器 / User Token Generator**.
3. Click **新增或移除 Threads 測試人員 / Add or Remove Threads Testers**.
4. Add your Threads handle.
5. **Open threads.net in a browser, log in as that handle, accept the invitation**
   (Settings → ... look for an invitation banner).

Without this, Meta will reject the OAuth approval with a generic "this app isn't
available" page.

### 3. Save your client credentials

From that same Settings panel, copy:

- **Threads 應用程式編號 / Threads App ID** → `THREADS_CLIENT_ID`
- **Threads 應用程式密鑰 / Threads App Secret** → `THREADS_CLIENT_SECRET`

```bash
cp .env.example .env
# edit .env to fill the two values
```

### 4. Deploy the Cloudflare Worker

This worker has two routes:

| Route                                  | Job                                                        |
| -------------------------------------- | ---------------------------------------------------------- |
| `GET /callback?code=...&state=...`     | Store `code` in KV keyed by `state` (TTL 10min)            |
| `GET /poll?state=...`                  | Return the stored code (then delete it); `204` if not yet  |

The Python CLI generates a random `state` each run, embeds it in the auth URL,
then polls `/poll` until the code arrives.

```bash
# one-time: install wrangler & log in (if you haven't)
npm i -g wrangler        # or: pnpm add -g wrangler
wrangler login

# first-time setup: copy the template, then create your own KV namespace
cd cloudflare-worker
cp wrangler.toml.example wrangler.toml
wrangler kv namespace create THREADS_AUTH
# -> copy the returned id into wrangler.toml under [[kv_namespaces]]

wrangler deploy
# Deployed threads-cb triggers https://threads-cb.<your-account>.workers.dev
```

Now put that URL into `.env`:

```ini
THREADS_REDIRECT_URI=https://threads-cb.<your-account>.workers.dev/callback
THREADS_WORKER_BASE=https://threads-cb.<your-account>.workers.dev
```

### 5. Register the callback URL in Meta -- with the chip-input gotcha

Back in **Use Cases → Customize → Access the Threads API → Settings**, fill:

| Field                            | Value                                                              |
| -------------------------------- | ------------------------------------------------------------------ |
| **重新導向回呼網址 (Redirect)**  | `https://threads-cb.<your-account>.workers.dev/callback`           |
| **解除安裝回呼網址 (Deauthorize)** | `https://threads-cb.<your-account>.workers.dev/deauthorize`        |
| **刪除回呼網址 (Delete)**        | `https://threads-cb.<your-account>.workers.dev/delete`             |

The deauthorize/delete URLs don't need to actually do anything for our purposes
-- but the form requires non-empty values.

> ⚠️ **The redirect URI field is a chip-input.** If you just paste and press
> **儲存 / Save**, you'll get:
>
> ```
> 無法儲存表單
> Redirect URIs: 請指定 OAuth 重新導向 URI。
> ```
>
> Fix: after pasting, **press Enter** so the string becomes a tag/chip,
> then press Save.

The form does *not* call your worker to verify -- it just checks URL shape.

### 6. First auth

```bash
make auth
```

This:

1. Generates a random `state`.
2. Prints the Threads OAuth URL and tries to open it in your browser.
3. You approve in the browser → Meta redirects to the worker → worker stashes the code.
4. The CLI polls `/poll?state=...` and picks the code up.
5. Exchanges code → short-lived token (1h).
6. **Immediately** exchanges short-lived → long-lived (60d).
7. Saves `THREADS_ACCESS_TOKEN`, `THREADS_USER_ID`, `THREADS_TOKEN_EXPIRES_AT` to `.env`.

### 7. Post something

```bash
make post MSG="hello world"
make list LIMIT=3
make delete ID=<thread_id>
```

---

## Token lifecycle

| Token type   | Endpoint                                                      | Lifetime | Notes                                              |
| ------------ | ------------------------------------------------------------- | -------- | -------------------------------------------------- |
| short-lived  | `POST /oauth/access_token` (with `code`)                      | 1 hour   | Exchange immediately to long-lived.                |
| long-lived   | `GET /access_token?grant_type=th_exchange_token`              | 60 days  | Returned by `make auth`.                           |
| refreshed    | `GET /refresh_access_token?grant_type=th_refresh_token`       | 60 days  | Use `make refresh` *before* the 60-day expiry.     |

**Practical rule:** run `make refresh` once a month; it renews to 60 days from
"now". If you let the long-lived token expire, you fall back to `make auth`.

`make whoami` always prints the remaining lifetime.

---

## API endpoints used

Base: `https://graph.threads.net/v1.0` (plus `https://graph.threads.net/oauth/...` for OAuth/token).

### Auth / token

| Op                 | Method | Path                                                                                | Notes |
| ------------------ | ------ | ----------------------------------------------------------------------------------- | ----- |
| authorize          | GET    | `https://threads.net/oauth/authorize?client_id=...&redirect_uri=...&scope=...&response_type=code&state=...` | browser flow |
| code → short token | POST   | `/oauth/access_token`                                                               | with `client_id`, `client_secret`, `code`, `redirect_uri`, `grant_type=authorization_code` |
| short → long      | GET    | `/access_token?grant_type=th_exchange_token`                                        | returns ~60d token |
| refresh long      | GET    | `/refresh_access_token?grant_type=th_refresh_token`                                 | another 60d |
| debug_token       | GET    | `/debug_token?input_token=...`                                                      | scope inspection |

### Publish (TEXT / IMAGE / VIDEO / CAROUSEL)

| Op                  | Method | Path                              | Notes                                                       |
| ------------------- | ------ | --------------------------------- | ----------------------------------------------------------- |
| create container    | POST   | `/{user-id}/threads`              | `media_type=TEXT\|IMAGE\|VIDEO\|CAROUSEL`                    |
| publish             | POST   | `/{user-id}/threads_publish`      | `creation_id=...`                                           |
| container status    | GET    | `/{container-id}?fields=status,error_message` | VIDEO / carousel children need `FINISHED` before publish |

Optional create-time params:
- `text` (max 500 chars), `link_attachment`, `image_url`, `video_url`, `alt_text`
- `topic_tag` (1-50 chars), `reply_to_id`, `quote_post_id`, `is_carousel_item`
- `reply_control` ∈ {`everyone`, `accounts_you_follow`, `mentioned_only`,
  `parent_post_author_only`, `followers_only`}
- `allowlisted_country_codes` (geo-gating, ISO codes csv)
- `location_id` (from `/location_search`)
- `children` (CAROUSEL only; csv of child container ids, 2-20)

### Read

| Op                      | Method | Path                                       | Notes                                          |
| ----------------------- | ------ | ------------------------------------------ | ---------------------------------------------- |
| me                      | GET    | `/me`                                      | `fields=id,username,...`                       |
| read one                | GET    | `/{thread-id}`                             | `fields=...`                                   |
| list mine               | GET    | `/{user-id}/threads`                       | `fields=...&limit=N`                           |
| my replies              | GET    | `/{user-id}/replies`                       | replies I authored                             |
| mentions of me          | GET    | `/{user-id}/mentions`                      | requires `threads_manage_mentions`             |
| publishing limit        | GET    | `/{user-id}/threads_publishing_limit`      | quota usage                                    |
| direct replies          | GET    | `/{thread-id}/replies`                     | `reverse=true\|false`                          |
| full conversation       | GET    | `/{thread-id}/conversation`                | whole tree                                     |
| pending / ignored       | GET    | `/{thread-id}/pending_replies`             | `approval_status=pending\|ignored`             |

### Manage / Delete

| Op           | Method | Path                              | Notes                                          |
| ------------ | ------ | --------------------------------- | ---------------------------------------------- |
| hide reply   | POST   | `/{reply-id}/manage_reply`        | `hide=true\|false`; nested replies follow      |
| delete       | DELETE | `/{thread-id}`                    | returns `{"success": true, ...}`               |

### Discover

| Op                | Method | Path                                                            | Notes                                       |
| ----------------- | ------ | --------------------------------------------------------------- | ------------------------------------------- |
| keyword search    | GET    | `/keyword_search?q=...&search_type=TOP\|RECENT`                  | needs `threads_keyword_search`              |
| location search   | GET    | `/location_search?q=...`                                        | for `location_id` lookups                   |
| location by id    | GET    | `/{location-id}`                                                | name/address/city/country/lat/lng           |
| oembed            | GET    | `/oembed?url=<permalink>&access_token=<APP_ID>\|<APP_SECRET>`    | public; uses app-token format               |

### Insights

| Op                  | Method | Path                                          | Default metrics                                                 |
| ------------------- | ------ | --------------------------------------------- | --------------------------------------------------------------- |
| post-level          | GET    | `/{thread-id}/insights?metric=...`            | `views,likes,replies,reposts,quotes,shares`                     |
| user-level          | GET    | `/{user-id}/threads_insights?metric=...`      | `views,likes,replies,reposts,quotes,followers_count`            |

User-level optional params: `since`, `until`, `breakdown=country|city|age|gender`
(for `follower_demographics`; requires ≥100 followers).

### Scopes by feature

| Scope                          | Required for                                                       |
| ------------------------------ | ------------------------------------------------------------------ |
| `threads_basic`                | every endpoint                                                     |
| `threads_content_publish`      | POST `/threads`, POST `/threads_publish`                           |
| `threads_delete`               | DELETE `/{thread-id}`                                              |
| `threads_manage_replies`       | POST `/manage_reply` (hide/unhide), pending_replies                |
| `threads_read_replies`         | GET `/replies`, `/conversation` (also covered by manage_replies)   |
| `threads_manage_insights`      | `/insights`, `/threads_insights`                                   |
| `threads_keyword_search`       | `/keyword_search`                                                  |
| `threads_manage_mentions`      | `/{user-id}/mentions`                                              |

### Endpoints gated behind Meta App Review

Some endpoints return `code 10` ("Application does not have permission for
this action") in dev mode even after a clean re-auth, because they need
**additional use cases enabled in the Meta App dashboard** and/or **App
Review** approval. Verified via probing on this account:

| Endpoint                      | Failure mode in dev mode             | What unblocks it                                                |
| ----------------------------- | ------------------------------------ | --------------------------------------------------------------- |
| `GET /keyword_search`         | code 10                              | Enable "Access Threads keyword search" use case + re-auth       |
| `GET /{user-id}/mentions`     | code 10                              | Enable "Access Threads mentions" use case + re-auth             |
| `GET /location_search`        | code 10                              | Enable Threads location use case + re-auth                      |
| `GET /{location-id}`          | code 10                              | Same as `location_search`                                       |
| `GET /oembed`                 | code 10 / subcode 4279067            | Submit app for review under "oEmbed Read"                       |
| `POST /{reply-id}/manage_reply` (`make hide`/`make unhide`) | code 10 / subcode 4279017 | App Review — even though the `threads_manage_replies` scope is granted, the action is gated |

What **does** work in dev mode with just the standard Threads tester role
(verified empirically by running `make` against each):
publish (TEXT/CAROUSEL with appropriate URLs), reply, quote, delete, list,
my-replies, replies, conversation, insights, user-insights, limits,
debug-token, refresh, whoami, smoke.

### Separate case: `pending-replies` and `limits`

- `GET /{thread-id}/pending_replies` returns `code 100` ("Reply approvals
  must be enabled for this media to access pending replies"). This is a
  **per-post toggle** in the Threads app — the post's author has to opt in
  to manual reply approvals for that thread, then this endpoint returns the
  queue. Not an App Review gate, just an opt-in feature.
- `GET /{user-id}/threads_publishing_limit` (`make limits`) works in dev
  mode but is **eventually-consistent**. Meta caches the counter values
  with roughly one-minute latency. Right after posting, the counter may
  read stale; re-run after a moment if it matters.

---

## Lessons learned (the actual ones that bit)

1. **`CLIENT_ID + CLIENT_SECRET` alone are useless.** Threads API is fully OAuth-
   user-scoped: every CRUD call needs a *user access token*, which only comes
   from a browser approval flow. There's no app-level "service account" token.
2. **The Threads redirect URI is a separate setting from generic Facebook Login OAuth.**
   The big 「授權回呼網址」 field under **App Settings → Advanced** is *not* it.
   The correct field lives under **Use Cases → Customize → Access the Threads API → Settings**.
3. **The Meta redirect URI input is a chip/tag input.** Paste alone doesn't
   commit it; you must press **Enter**. The error message ("請指定 OAuth 重新導向
   URI") is misleading -- it says "specify a URI" even though you can see one in
   the box.
4. **A Cloudflare Worker is the lowest-friction `redirect_uri` for solo dev.**
   No domain, no tunnel, no local server. Stable HTTPS in ~30s. Use `state` as
   both CSRF token *and* KV lookup key, so the Python script picks up its own
   code automatically.
5. **Container + publish is two API calls, not one.** It exists so you can
   batch carousels. For plain TEXT it's basically instant -- ~2s pause is
   plenty.
6. **`media_type=TEXT` goes in; `TEXT_POST` comes back.** Don't be confused by
   the asymmetry.
7. **No UPDATE.** Threads posts are immutable after publish -- the `U` in CRUD
   is just N/A. Plan for "delete + re-post" if you need editing.
8. **Delete returns HTTP 200 with `{"success": true}`; the subsequent GET on
   the same id returns HTTP 400, not 404.** Trust the `success` flag, not the
   later GET status.
9. **Always exchange short → long immediately.** Short-lived tokens last 1
   hour; you'll trip over them constantly otherwise. `make auth` does this in
   one shot.
10. **The OAuth `code` is 1-hour valid and single-use.** If the worker captured
    one and you didn't exchange it, just `make auth` again.
11. **`debug_token` lists the *granted* scopes, which may be a subset of what
    you requested.** Meta silently drops scopes whose use cases aren't
    enabled on the app. Always check `make debug-token` after a fresh `make
    auth` to confirm what you actually got.
12. **`code 10` (not 401, not scope-error) almost always means App Review.**
    Re-OAuth won't help. Either enable the relevant use case in the dashboard
    or submit the app for review. Subcodes like `4279017` (manage_reply) and
    `4279067` (oembed) are the specific feature gates.
13. **`limits` is eventually-consistent.** Right after posting, `quota_usage`
    and `reply_quota_usage` may lag by ~a minute. Re-read after a moment.
14. **`quote` is a top-level post, not a reply.** A quote shows up in your
    main feed and counts against `quota_usage`, not `reply_quota_usage`. The
    differentiator is whether the request carries `reply_to_id` (yes →
    reply) vs `quote_post_id` (yes → top-level with a quote attachment).
15. **`pending_replies` is per-post opt-in**, not a feature gate. It returns
    `code 100` unless the thread's author enabled reply approvals on that
    specific post inside the Threads app.
