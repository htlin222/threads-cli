# Worker cron setup — scheduled posting to Threads

Next-step runbook for turning the Cloudflare Worker into a **scheduled poster**. The
code is already in the repo (`cloudflare-worker/src/index.js` `scheduled()` handler);
this guide is the operator checklist to make it live.

> Background and design rationale live in [`../TUTORIAL.md`](../TUTORIAL.md) >
> "Scheduled posting (Cloudflare cron)". This file is the do-it checklist.

## What it does

On each cron tick the worker:

1. Reads the long-lived token from **KV** (not `.env`) and refreshes it if it's within
   1 day of expiry, writing the new token back to KV.
2. Fetches `FEED_URL` (JSON Feed, RSS, or Atom).
3. Posts the **newest item it hasn't posted before** — one per tick.
4. Records the item key in a rolling 200-entry `feed:seen` set in KV (de-dup that
   survives feed reordering, bursts of new items, and republished entries).

No Meta client secret is needed at runtime; token refresh uses only the token.

## Prerequisites

- Worker already deployed once for OAuth (`make worker-deploy` works).
- A valid long-lived token in `.env` — run `make whoami`; if expired, `make auth`.
- `wrangler` logged in (`wrangler whoami`).

## Steps

### 1. Seed the token into KV

Reads `.env` → writes `token:access` / `token:user_id` / `token:expires_at` to the
`THREADS_AUTH` KV namespace. Run it right after a fresh `make auth` / `make refresh`.

```bash
make worker-seed-token
```

Verify (prints your numeric user id):

```bash
cd cloudflare-worker && wrangler kv key get --remote --binding THREADS_AUTH token:user_id
```

### 2. Configure the trigger and feed

Edit `cloudflare-worker/wrangler.toml` (gitignored — your private copy):

```toml
[triggers]
crons = ["0 * * * *"]   # hourly. Daily 09:00 UTC -> "0 9 * * *". Weekly Mon 09:00 -> "0 9 * * 1"

[vars]
FEED_URL = "https://your-real-feed.example/feed.json"   # JSON Feed, RSS, or Atom
DRY_RUN  = "1"          # keep "1" for the first test run
```

If you forked fresh, copy the template first: `cp wrangler.toml.example wrangler.toml`
and paste your KV id (`wrangler kv namespace create THREADS_AUTH` returns it).

### 3. Deploy in dry-run and test

```bash
make worker-deploy
```

Trigger one run from the Cloudflare dashboard (Workers → `threads-cb` → Triggers →
"Trigger scheduled event"), or test locally:

```bash
cd cloudflare-worker && wrangler dev --test-scheduled
# in another shell:
curl "http://localhost:8787/__scheduled?cron=0+*+*+*+*"
```

Watch the log — with `DRY_RUN="1"` you should see the post it *would* make, no publish:

```bash
make worker-tail
# cron DRY_RUN: would post (key=...): <title> \n\n <link>
```

### 4. Go live

Set `DRY_RUN = "0"` in `wrangler.toml`, then:

```bash
make worker-deploy
```

Confirm a real post landed:

```bash
make worker-tail            # cron: posted thread <id> (key=...)
make list LIMIT=5           # cross-check from the CLI
```

## Operating notes

- **Volume vs quota:** hourly + one-item-per-tick = ≤24 posts/day, far under the
  250/24h cap. Most ticks post nothing (`cron: no new item`).
- **Token ownership shifts to the worker.** After it self-refreshes, the token in your
  local `.env` is stale — expected. Only re-run `make worker-seed-token` if you revoke
  the token and `make auth` again.
- **Inspect / reset de-dup state:**
  ```bash
  cd cloudflare-worker
  wrangler kv key get    --remote --binding THREADS_AUTH feed:seen      # what's been posted
  wrangler kv key delete --remote --binding THREADS_AUTH feed:seen      # replay feed from scratch
  ```
- **Turn it off:** remove the `[triggers]` block from `wrangler.toml` and redeploy, or
  `make worker-delete` to remove the whole worker.

## Troubleshooting

| Log line | Cause | Fix |
| --- | --- | --- |
| `cron: no token/user_id in KV` | KV not seeded | `make worker-seed-token` |
| `refresh failed: ...` | token expired >60d | `make auth`, then re-seed |
| `feed fetch failed: HTTP 4xx/5xx` | bad `FEED_URL` or feed down | check the URL in a browser |
| `cron: feed empty` | parser found no items | confirm it's JSON Feed / RSS / Atom |
| `POST .../threads failed: ... code 190` | auth — worker auto-retries once after refresh; if it persists, re-seed | `make worker-seed-token` |
| posts the same item repeatedly | feed has no stable `guid`/`id` and the title/link changes each fetch | give the feed stable ids, or accept SHA-256(link+title) keying |

## Possible next steps (not built)

- Swap `formatPost()` for an LLM summarizer (would add an `ANTHROPIC_API_KEY` secret via
  `wrangler secret put` — never in `wrangler.toml`).
- Post more than one item per tick (loosen the `find(...)` to a capped loop in `runCron`).
- Mirror the refreshed token back into `.env` so the CLI and worker stay in sync.
