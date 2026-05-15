# threads-cli

Minimal Threads API client with a Cloudflare Worker as the OAuth callback relay.
Covers most of the official API surface from the command line: publish (text /
image / video / carousel / reply / quote), read, search, manage replies, get
insights, and look up locations.

```
.
├── threads.py             # thin CLI dispatcher (argparse + cmd_* + main with auto-retry)
├── threads_lib.py         # all helpers, typed errors, validators, OAuth, HTTP
├── tests/
│   └── test_threads_lib.py  # pytest suite (58 tests, no network)
├── pytest.ini
├── Makefile               # human-friendly wrappers around threads.py
├── cloudflare-worker/     # Wrangler project: /callback stashes ?code= in KV; /poll returns it
│   ├── src/index.js
│   └── wrangler.toml
├── .env                   # secrets + token (gitignored)
├── .env.example
├── CLAUDE.md              # next-session playbook for Claude Code
├── .claude/skills/onboarding/SKILL.md
├── TUTORIAL.md            # zero-to-posting walkthrough + every gotcha + endpoint table
└── README.md
```

## Tests

```bash
make test
```

Runs `pytest tests/` via `uv run` with `pytest` + `responses` (mocks
`requests` so nothing leaves the machine). Covers pure helpers (Config,
validators, build_post_params, fmt_expiry, ...) and HTTP behavior
(error parsing, 401 → AuthError, container ready loop, worker polling).

## Quick start (after one-time setup -- see TUTORIAL.md)

```bash
make post MSG="hello world"
make help                   # full target list
```

If a call returns a 401 / token-expired / scope error:

```bash
make refresh                # extends the long-lived (60d) token
make auth                   # full OAuth flow (open browser; if token >60d or scopes changed)
```

## Target cheat sheet (group → make targets)

| Group                | Targets                                                                            |
| -------------------- | ---------------------------------------------------------------------------------- |
| **Publish**          | `post`, `post-image`, `post-video`, `post-carousel`, `reply`, `quote`              |
| Publish modifiers    | `LINK=`, `TOPIC=`, `ALT=`, `REPLY_CONTROL=`, `COUNTRIES=`, `LOCATION=`             |
| **Read mine**        | `list`, `my-replies`, `mentions`, `limits`                                         |
| **Read replies**     | `replies`, `conversation`, `pending-replies`                                       |
| **Manage**           | `hide`, `unhide`, `delete`                                                         |
| **Discover**         | `search`, `location-search`, `location-get`, `oembed`                              |
| **Insights**         | `insights`, `user-insights`                                                        |
| **Token / auth**     | `auth`, `refresh`, `whoami`, `debug-token`                                         |
| **Worker / env**     | `worker-deploy`, `worker-tail`, `env-check`, `smoke`                               |

`make help` always prints the up-to-date list with usage.

## First-time setup

See **TUTORIAL.md**. The short version:

1. Create a Meta app, enable **Use Cases → Access the Threads API**.
2. Add yourself as a **Threads tester** and accept on threads.net.
3. `cp .env.example .env`; fill `THREADS_CLIENT_ID` / `THREADS_CLIENT_SECRET`.
4. `make worker-deploy` (worker code is committed; KV namespace ID in
   `wrangler.toml` is tied to *this* account -- if you fork, create your own
   via `wrangler kv namespace create THREADS_AUTH`).
5. Register `https://<your-worker>.workers.dev/callback` under **Use Cases →
   Customize → Settings → Redirect Callback URLs**. ⚠️ The field is a
   chip-input: type the URL then **press Enter** before saving.
6. `make auth` -- approves in browser, worker captures `code`, script exchanges
   for a 60-day long-lived token.
7. `make post MSG="hello world"`.

## SCOPES requested by `make auth`

```
threads_basic, threads_content_publish, threads_delete,
threads_manage_replies, threads_read_replies, threads_manage_insights,
threads_keyword_search, threads_manage_mentions
```

If your Meta app hasn't enabled all of these use cases yet, OAuth may
silently drop missing ones (or fail with "invalid scope"). Either enable them
in the dashboard or narrow the set:
`uv run threads.py auth --scopes="threads_basic,threads_content_publish,..."`.

## Endpoints gated behind extra Meta-side approval

Some targets return `code 10` ("Application does not have permission") in
dev mode even after a clean re-auth — they need additional **use cases
enabled in the Meta App dashboard** and/or **App Review**:

| Target                | Required Meta-side action                                    |
| --------------------- | ------------------------------------------------------------ |
| `make search`         | enable "Access Threads keyword search" use case + re-auth    |
| `make mentions`       | enable "Access Threads mentions" use case + re-auth          |
| `make location-search` / `make location-get` | enable Threads location use case + re-auth  |
| `make oembed`         | submit app for review under "oEmbed Read"                    |
| `make hide` / `make unhide` | submit app for review — `threads_manage_replies` scope alone isn't enough for `/manage_reply` POSTs |

And one separate case: `make pending-replies` returns `code 100` ("Reply
approvals must be enabled") because that's a **per-post opt-in** in the
Threads app, not a global gate. Only works on threads where the author
turned reply approvals on for that specific post.

`make limits` is eventually-consistent — Meta caches the counter values with
~minute latency. Re-read if numbers look stale right after a post.

Everything else (publish / read / insights / manage-reply / list / etc.)
works in dev mode with just the standard tester role.

## Notes

- Threads has no UPDATE -- posts are immutable. Delete + re-post if you need editing.
- Container + publish is two API calls (enables carousels). `post-video` and
  carousel children auto-wait for `status=FINISHED`.
- The Worker's `/poll` endpoint returns the OAuth code once and deletes it from KV.
- Long-lived tokens are good for 60 days; `make refresh` extends to another 60.

The 10 hard-won gotchas (Meta dashboard chip input, `TEXT` vs `TEXT_POST`,
delete returns 200 then GET returns 400 not 404, etc.) all live in **TUTORIAL.md**
under "Lessons learned".
