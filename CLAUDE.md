# CLAUDE.md -- instructions for Claude Code in this repo

This repo is a CLI client for the Meta **Threads API** plus a Cloudflare
Worker as the OAuth callback relay. The user's typical request is *"post X"*,
*"search X"*, *"看 insights"*, etc. -- translate to `make` and report results.

---

## TL;DR command map

| User says...                                                    | You run                                                |
| --------------------------------------------------------------- | ------------------------------------------------------ |
| "post X" / "發 X"                                                | `make post MSG="X"`                                    |
| "post 圖 <url>" / "post image"                                   | `make post-image URL=<url> [MSG="..."]`                |
| "post 影片 <url>" / "post video"                                 | `make post-video URL=<url> [MSG="..."]`                |
| "carousel" / "多圖" + URLs                                       | `make post-carousel URLS="u1,u2,..." [MSG=...]`        |
| "reply <id> X" / "回 <id> X"                                     | `make reply TO=<id> MSG="X"`                           |
| "quote <id> X" / "引用 <id>"                                     | `make quote OF=<id> MSG="X"`                           |
| "delete <id>" / "刪 <id>"                                        | `make delete ID=<id>`                                  |
| "list" / "最近的貼文"                                            | `make list LIMIT=10`                                   |
| "我發的 replies" / "my replies"                                  | `make my-replies`                                      |
| "mentions" / "誰 @ 我"                                           | `make mentions`                                        |
| "看 <id> 的 replies"                                             | `make replies ID=<id>`                                 |
| "看 <id> 的 conversation"                                        | `make conversation ID=<id>`                            |
| "待審回覆" / "pending"                                            | `make pending-replies ID=<id>`                         |
| "隱藏回覆 <id>" / "hide <id>"                                     | `make hide ID=<id>`                                    |
| "取消隱藏 <id>" / "unhide <id>"                                   | `make unhide ID=<id>`                                  |
| "搜尋 X" / "search X"                                            | `make search Q="X"`                                    |
| "找地點 X" / "location X"                                        | `make location-search Q="X"`                           |
| "insights <id>" / "看數據"                                       | `make insights ID=<id>`                                |
| "我的 insights"                                                  | `make user-insights`                                   |
| "限制" / "quota" / "limits"                                       | `make limits`                                          |
| "embed <permalink>"                                              | `make oembed URL=<permalink>`                          |
| "whoami" / "token 還在嗎"                                         | `make whoami`                                          |
| "refresh token"                                                  | `make refresh`                                         |
| "re-auth" / "重抓 token" / token expired                          | `make auth`                                            |
| "smoke test"                                                     | `make smoke`                                           |

`make help` always prints the up-to-date list. Run from the repo root.

### Common modifier env vars (compose with publish targets)

```
LINK="https://..."           link preview (TEXT only)
TOPIC="ai"                   topic tag (1-50 chars)
ALT="alt text"               alt text on image/video
REPLY_CONTROL=everyone|accounts_you_follow|mentioned_only|followers_only|parent_post_author_only
COUNTRIES=TW,JP              geo-gating (ISO codes)
LOCATION=<loc-id>            from `make location-search`
```

Example: `make post MSG="hi" LINK=https://example.com TOPIC=ai COUNTRIES=TW,JP`

## Handling "post X"

1. **Don't ask clarifying questions** unless the message is genuinely ambiguous.
2. Run `make post MSG="<text>"` (escape quotes / `$` for shell).
3. On success, reply with **just the permalink** from the JSON response.
4. **The CLI now auto-recovers on 401**: it pre-flights a refresh when the
   token has <5 min left, and catches `AuthError` from any subcommand, refreshes
   the long-lived token, and retries once. You shouldn't need to chain
   `make refresh && make post` manually — that's wired up in `threads.py:main()`.
5. If the auto-refresh **also** fails (`refresh also failed` message): the
   long-lived token has expired >60d. Tell user a browser approval is needed,
   run `make auth`, then retry.
6. Don't auto-delete the post.

If the user asks for a **scope they currently don't have** (e.g. *"看 insights"*
right after first auth without insights scope), Meta will return a permission
error. Resolution: ensure the scope is in `SCOPES` in `threads.py`, then run
`make auth` again so the user approves the wider set.

## Current SCOPES requested by `make auth`

```
threads_basic, threads_content_publish, threads_delete,
threads_manage_replies, threads_read_replies, threads_manage_insights,
threads_keyword_search, threads_manage_mentions
```

If you add a feature that needs another scope, edit `SCOPES` in `threads.py`
and tell the user they need to re-run `make auth`.

## Endpoints gated behind extra Meta-side approval

These return `code 10` ("Application does not have permission for this
action") in dev mode even with a fresh re-auth, because they need additional
**use cases enabled in the Meta App dashboard** and/or **App Review**:

| Make target          | Failure                                       | Required                                                       |
| -------------------- | --------------------------------------------- | -------------------------------------------------------------- |
| `make search`        | `code 10`                                     | Use case "Access Threads keyword search" enabled + scope granted |
| `make mentions`      | `code 10`                                     | Use case "Access Threads mentions" + scope granted              |
| `make location-search` / `make location-get` | `code 10`             | Threads location use case enabled                              |
| `make oembed`        | `code 10` / `subcode 4279067`                 | Full App Review under "oEmbed Read"                            |
| `make hide` / `make unhide` (`/manage_reply`) | `code 10` / `subcode 4279017` | App Review even though `threads_manage_replies` scope is granted |
| `make pending-replies` | `code 100` "Reply approvals must be enabled" | Per-post toggle — only works on threads where the author opted in to reply approvals |
| `make replies` | `code 100` "app is in dev mode" | App must be in production mode. The CLI auto-falls back to `/{id}/conversation` and filters direct children, so `make replies` still works transparently — you'll see one `note: /replies gated…` line on stderr. |
| `make limits` | (works, but values lag) | Eventually-consistent; Meta caches ~minute latency. Re-read in a bit if numbers look off. |

If the user hits `code 10` on these, the fix is **not** in this repo — they
need to enable the use case in the Meta dashboard, then `make auth` again.
Don't try to "fix" the script; tell them what's gated and link to
TUTORIAL.md > "Endpoints gated behind Meta App Review".

## Token state in `.env`

```
THREADS_CLIENT_ID, THREADS_CLIENT_SECRET     # static; from Meta dashboard
THREADS_REDIRECT_URI, THREADS_WORKER_BASE    # static; the Cloudflare worker URL
THREADS_ACCESS_TOKEN                         # long-lived (60d), refreshable
THREADS_USER_ID
THREADS_TOKEN_EXPIRES_AT                     # unix timestamp; `make whoami` shows remaining
```

**Never** print the full client secret or full access token in chat. Last 6-8
chars only when you need to show progress.

## Cloudflare Worker

- Deployed URL is stored in `$THREADS_WORKER_BASE` in `.env` (gitignored). Don't paste the literal URL here.
- Source: `cloudflare-worker/src/index.js`. Config: `cloudflare-worker/wrangler.toml` (gitignored; copy from `wrangler.toml.example`).
- **Don't redeploy** unless source/config changed. If needed: `make worker-deploy`.
- KV namespace `THREADS_AUTH` stores OAuth codes keyed by `state` (TTL 10min).
- Logs: `make worker-tail`.
- **Scheduled posting** (optional): the worker's `scheduled()` handler cron-posts the
  newest item from `FEED_URL` to Threads. Token lives in KV (`token:*` keys) and
  self-refreshes; de-dup via `feed:seen`. Setup is `make worker-seed-token` + a
  `[triggers]`/`[vars]` block in `wrangler.toml`. Start with `DRY_RUN="1"`. Full
  walkthrough: TUTORIAL.md > "Scheduled posting (Cloudflare cron)". This is independent
  of the OAuth-relay routes — don't confuse the `code:<state>` keys (OAuth) with the
  `token:*` / `feed:seen` keys (cron).

## Recovery / debug ladder

1. `make env-check` — required keys present?
2. `make whoami` — token still valid? (also prints expiry)
3. `make refresh` — extends long-lived token; retry the call.
4. `make auth` — full OAuth (browser approval).
5. `make worker-tail` then re-run `make auth` — verify worker route + Meta-side
   redirect URI in `.env` still match.

## Quota model

`make limits` returns two independent counters; don't confuse them.

| Counter                | Limit  | Counts                                                                |
| ---------------------- | ------ | --------------------------------------------------------------------- |
| `quota_usage`          | 250/24h | Top-level posts — TEXT/IMAGE/VIDEO/CAROUSEL + **quotes** (no `reply_to_id`) |
| `reply_quota_usage`    | 1000/24h | Anything with `reply_to_id` (i.e. `make reply ...`)                 |

Rolling 24h window, not calendar-day. Per-user, not per-app.

## Code layout

```
threads.py        Thin CLI dispatcher; argparse + cmd_* + main() with auto-retry
threads_lib.py    All helpers, typed errors (ApiError, AuthError, ValidationError),
                  validators, OAuth flow, HTTP wrappers. Pure functions are testable.
tests/
  test_threads_lib.py  pytest; 58 tests; runs via `make test` (no network)
```

When adding a new endpoint:
1. Put the helper in `threads_lib.py` (raises `ApiError` on failure, no `sys.exit`).
2. Add a thin `cmd_*` in `threads.py` that calls it.
3. Add an argparse subparser + Make target.
4. Add a pytest test for the helper (use `responses` to mock HTTP).

### Reading long output from Claude Code

When invoked via Claude Code's Bash tool, `make <target>` output is silently
truncated around line 50 with `(N lines truncated)` — the Makefile and script
aren't doing this, it's the harness. The data is all there; you just can't
see it. To get the full output:

- Call the script directly: `uv run threads.py conversation <id>` (skips
  `make`'s wrapper and avoids the cut), **or**
- Redirect to a tmp file and Read it: `make conversation ID=<id> > /tmp/out.txt 2>&1`.

`_print_thread_list` is unpaginated, so the file always contains the full
response. Use this for any list reader: `conversation`, `my-replies`,
`mentions`, `pending-replies`.

## Anti-patterns

- ❌ Creating new Python scripts. `threads.py` already has 20 subcommands; add a
  subcommand there.
- ❌ Pre-emptively spawning OAuth. Just call the API; recover from 401.
- ❌ Committing `.env`. It's in `.gitignore`.
- ❌ Printing full secrets/tokens.
- ❌ Trying to "edit" a posted thread. Threads has no UPDATE — delete + repost.
- ❌ Re-deploying the worker for trivial reasons.

## Long-form context

- `TUTORIAL.md` — zero-to-posting walkthrough + every gotcha + API endpoint table.
- `README.md` — user-facing intro + target cheat sheet.

If this file and the `onboarding` skill drift, the skill is canonical for
runtime behavior — sync this file to match.
