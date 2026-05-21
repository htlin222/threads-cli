---
name: onboarding
description: Use when entering the threads-cli repo or when the user asks to post / search / reply / quote / hide / read insights / look up mentions or location on Threads, manage tokens, or asks "how do I X" about this project. Maps every common intent to a single `make` target and defines the recovery flow when the token or scope is wrong.
---

# Onboarding: threads-cli

This repo is a CLI client for the Meta **Threads API**, plus a Cloudflare
Worker that acts as the OAuth callback relay. The user's normal request is
*"post X"* / *"search X"* / *"看 insights"* — translate it into `make` and
report results.

## Intent → command (full playbook)

### Publish
| User says...                                  | You run                                              |
| --------------------------------------------- | ---------------------------------------------------- |
| "post X" / "發 X"                              | `make post MSG="X"`                                  |
| "post image / 圖 <url>"                        | `make post-image URL=<url> [MSG="..."]`              |
| "post video / 影片 <url>"                      | `make post-video URL=<url> [MSG="..."]`              |
| "carousel" + urls                              | `make post-carousel URLS="u1,u2,..." [MSG=...]`      |
| "reply <id> X" / "回 <id> X"                   | `make reply TO=<id> MSG="X"`                         |
| "quote <id> X" / "引用 <id>"                   | `make quote OF=<id> MSG="X"`                         |

Modifier env vars work on any publish target:
`LINK=<url>`, `TOPIC=<tag>`, `ALT=<text>`, `REPLY_CONTROL={everyone|accounts_you_follow|mentioned_only|followers_only|parent_post_author_only}`,
`COUNTRIES=TW,JP` (geo-gating), `LOCATION=<loc-id>`.

### Read mine
| Intent                  | Target                            |
| ----------------------- | --------------------------------- |
| my threads              | `make list [LIMIT=10]`            |
| my replies              | `make my-replies`                 |
| mentions of me          | `make mentions`                   |
| quota / publishing limit | `make limits`                    |

### Read replies / conversations
| Intent                                  | Target                                                |
| --------------------------------------- | ----------------------------------------------------- |
| direct replies on a thread              | `make replies ID=<thread-id>`                         |
| full conversation tree                  | `make conversation ID=<thread-id>`                    |
| pending or ignored replies              | `make pending-replies ID=<thread-id> [STATUS=pending|ignored]` |

### Manage
| Intent                | Target                          |
| --------------------- | ------------------------------- |
| hide a reply          | `make hide ID=<reply-id>`       |
| unhide a reply        | `make unhide ID=<reply-id>`     |
| delete                | `make delete ID=<thread-id>`    |

### Discover
| Intent                          | Target                                              |
| ------------------------------- | --------------------------------------------------- |
| keyword search public threads   | `make search Q="..." [TYPE=TOP|RECENT]`             |
| find a location id              | `make location-search Q="..."`                      |
| location details                | `make location-get ID=<loc-id>`                     |
| embed HTML for a permalink      | `make oembed URL=<permalink>`                       |

### Insights
| Intent                | Target                                                              |
| --------------------- | ------------------------------------------------------------------- |
| single-post insights  | `make insights ID=<thread-id> [METRIC=views,likes,...]`             |
| user insights         | `make user-insights [METRIC=...] [SINCE=ts] [UNTIL=ts] [BREAKDOWN=...]` |

### Token / auth
| Intent                       | Target              |
| ---------------------------- | ------------------- |
| GET /me + expiry             | `make whoami`       |
| refresh long-lived token     | `make refresh`      |
| token introspect             | `make debug-token`  |
| re-OAuth (>60d / new scopes) | `make auth`         |

## Handling "post X" (most common)

1. **Don't ask clarifying questions** unless the message is empty/ambiguous.
2. Run `make post MSG="<text>"` (escape quotes / `$` for shell safety).
3. On success, reply with **just the permalink** from the JSON response.
4. **The CLI auto-recovers on 401** — `threads.py:main()` pre-flights a
   refresh if the token has <5 min left and retries once after any
   `AuthError`. You don't need to chain `make refresh && make post` manually.
5. If you see `refresh also failed` in stderr: token is >60d expired. Tell the
   user a browser approval is needed, run `make auth`, then retry.
6. **Don't** delete the post unless asked.

## SCOPES requested by `make auth`

```
threads_basic, threads_content_publish, threads_delete,
threads_manage_replies, threads_read_replies, threads_manage_insights,
threads_keyword_search, threads_manage_mentions
```

If Meta rejects OAuth with "invalid scope", the user's app might not have that
use case enabled. Narrow with `uv run threads.py auth --scopes="<csv>"` and
ask them to enable the missing use case in the Meta dashboard.

## Endpoints gated behind extra Meta-side approval

These return `code 10` ("Application does not have permission") in dev mode
even after re-auth — they need **additional use cases enabled in the Meta App
dashboard** and/or **App Review**:

- `make search` (keyword search)
- `make mentions`
- `make location-search`, `make location-get`
- `make oembed` (needs full App Review under "oEmbed Read")
- `make hide` / `make unhide` (manage_reply POST is gated even with the scope; subcode 4279017)

`make replies` returns `code 100` "The app associated with this request is in
dev mode" — `/{id}/replies` requires the app to be promoted to production
mode. The CLI auto-falls back to `/{id}/conversation` and filters direct
children, so `make replies` still works; you'll see one stderr line `note:
/replies gated in dev mode, falling back to /conversation`. No action needed
unless you want the raw `/replies` response.

`make pending-replies` returns `code 100` ("Reply approvals must be enabled
for this media") — it's a **per-post opt-in**, not a global gate. Only works
if the thread's author enabled reply approvals on that specific post.

`make limits` is **eventually-consistent**: Meta caches the values with
~minute latency. If the numbers look stale right after posting, re-read.

If a user hits `code 10` on these, the fix is **not** in this repo. Tell them:
1. Go to Meta App Dashboard → Use Cases → enable the matching use case.
2. Then `make auth` again to get the broader scope set.
3. For `oembed`, also submit the app for review under "oEmbed Read".

Don't try to fix the script. The error is intentional on Meta's side.

## State in `.env` (gitignored)

```
THREADS_CLIENT_ID, THREADS_CLIENT_SECRET     # static
THREADS_REDIRECT_URI, THREADS_WORKER_BASE    # static; the worker URL
THREADS_ACCESS_TOKEN                         # long-lived (60d), refreshable
THREADS_USER_ID
THREADS_TOKEN_EXPIRES_AT                     # unix ts; `make whoami` shows remaining
```

**Never** print full secrets/tokens. Last 6-8 chars only when needed.

## Cloudflare Worker

- Already deployed; URL is in `$THREADS_WORKER_BASE` in `.env` (gitignored). Don't paste the literal URL here.
- Source: `cloudflare-worker/src/index.js`. Config: `cloudflare-worker/wrangler.toml` (gitignored; copy from `wrangler.toml.example`).
- KV namespace `THREADS_AUTH` stores OAuth codes keyed by `state` (TTL 10min).
- **Don't redeploy** unless code/config actually changed.
- Logs: `make worker-tail`.

## Recovery / debug ladder

1. `make env-check` — required keys present in `.env`?
2. `make whoami` — token still valid? (prints expiry too)
3. `make refresh` — extends long-lived token; retry the user's request.
4. `make auth` — full OAuth flow (browser approval).
5. `make worker-tail` then re-run `make auth` — if worker `/poll` times out,
   verify the Meta-dashboard redirect URI still matches `THREADS_REDIRECT_URI`.

## Quota model (from `make limits`)

| Counter             | Limit    | Counts                                                              |
| ------------------- | -------- | ------------------------------------------------------------------- |
| `quota_usage`       | 250/24h  | Top-level posts — TEXT/IMAGE/VIDEO/CAROUSEL + **quotes**            |
| `reply_quota_usage` | 1000/24h | Posts with `reply_to_id` (i.e. `make reply ...`)                    |

Rolling 24h window, per-user. Quotes are top-level (count against the 250).

## Code layout

```
threads.py         Thin CLI dispatcher; argparse + cmd_* + main() w/ auto-retry
threads_lib.py     All helpers, typed errors, validators, OAuth, HTTP wrappers
tests/             pytest suite; `make test` (no network)
```

Adding endpoints: helper in `threads_lib.py` (raises `ApiError`/`ValidationError`,
never calls `sys.exit`), thin `cmd_*` in `threads.py`, argparse subparser, Make
target, pytest test with `responses`-mocked HTTP.

### Reading long output from Claude Code

When invoked via Claude Code's Bash tool, `make <target>` output is silently
truncated around line 50 with `(N lines truncated)` — the harness, not the
script, is doing it. The full data is there; you just can't see it. To
recover the full output:

- Call the script directly: `uv run threads.py conversation <id>` (bypasses
  `make`'s wrapper and the truncation), **or**
- Redirect to a tmp file and Read it: `make conversation ID=<id> > /tmp/out.txt 2>&1`.

`_print_thread_list` is unpaginated, so the file always contains the full
response. Use this for any list reader (`conversation`, `my-replies`,
`mentions`, `pending-replies`, `replies`).

## Anti-patterns

- ❌ Creating new Python scripts. `threads.py` has 20 subcommands; add one.
- ❌ Spawning OAuth proactively. Call the API and recover on 401.
- ❌ Committing `.env`. It's in `.gitignore` — keep it that way.
- ❌ Printing full secrets/tokens.
- ❌ Trying to "edit" a posted thread. Threads has **no UPDATE** — delete + repost.
- ❌ Re-deploying the worker for trivial reasons.

## Long-form context

- `TUTORIAL.md` — zero-to-posting walkthrough + every gotcha + API endpoint table.
- `README.md` — user-facing intro + target cheat sheet.
- `CLAUDE.md` — playbook in repo-level form (mirror of this skill).

If `CLAUDE.md` and this skill ever drift, **this skill wins** for runtime
behavior; sync `CLAUDE.md` to match.
