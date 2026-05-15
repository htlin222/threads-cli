.DEFAULT_GOAL := help
SHELL := /bin/bash

# Prefer uv (pulls deps from PEP-723 header on first run); fall back to python3.
RUN := $(shell command -v uv >/dev/null 2>&1 && echo "uv run" || echo "python3")

# Build optional post flags from environment variables. Empty vars -> empty flags.
POST_FLAGS := \
  $(if $(LINK),--link "$(LINK)") \
  $(if $(TOPIC),--topic "$(TOPIC)") \
  $(if $(REPLY_CONTROL),--reply-control "$(REPLY_CONTROL)") \
  $(if $(COUNTRIES),--countries "$(COUNTRIES)") \
  $(if $(LOCATION),--location "$(LOCATION)") \
  $(if $(ALT),--alt-text "$(ALT)")

.PHONY: help \
  post post-image post-video post-carousel reply quote \
  list my-replies mentions limits \
  replies conversation pending-replies \
  hide unhide delete \
  search location-search location-get oembed \
  insights user-insights \
  auth refresh whoami debug-token \
  smoke worker-deploy worker-tail worker-delete env-check test

help: ## show this help
	@echo "Threads API CLI -- 'make <target>' wraps 'uv run threads.py <subcmd>'"
	@echo ""
	@echo "Publish:"
	@echo "  make post MSG=\"...\"                            text post"
	@echo "  make post-image URL=<img> [MSG=\"...\"]           image post"
	@echo "  make post-video URL=<video> [MSG=\"...\"]         video post (auto-waits for transcode)"
	@echo "  make post-carousel URLS=\"u1,u2,...\" [MSG=...]    2-20 item carousel"
	@echo "  make reply TO=<id> MSG=\"...\"                    reply to a thread"
	@echo "  make quote OF=<id> MSG=\"...\"                    quote a thread"
	@echo "  Optional on any of the above:"
	@echo "    LINK=<url> TOPIC=<tag> ALT=<text>"
	@echo "    REPLY_CONTROL={everyone|accounts_you_follow|mentioned_only|"
	@echo "                   parent_post_author_only|followers_only}"
	@echo "    COUNTRIES=TW,JP   (geo-gating)"
	@echo "    LOCATION=<loc-id>  (from \`make location-search Q=...\`)"
	@echo ""
	@echo "Read mine:"
	@echo "  make list [LIMIT=10]              my recent threads"
	@echo "  make my-replies [LIMIT=10]        my replies"
	@echo "  make mentions [LIMIT=10]          mentions of me"
	@echo "  make limits                       quota / publishing limit"
	@echo ""
	@echo "Read replies / conversations:"
	@echo "  make replies ID=<thread-id>       direct replies on a thread"
	@echo "  make conversation ID=<thread-id>  full conversation tree"
	@echo "  make pending-replies ID=<thread-id> [STATUS=pending|ignored]"
	@echo ""
	@echo "Manage:"
	@echo "  make hide ID=<reply-id>           hide a reply (nested too)"
	@echo "  make unhide ID=<reply-id>         unhide"
	@echo "  make delete ID=<thread-id>        delete a thread or reply"
	@echo ""
	@echo "Discover:"
	@echo "  make search Q=\"...\" [TYPE=TOP|RECENT]   keyword search public threads"
	@echo "  make location-search Q=\"...\"            find location ids"
	@echo "  make location-get ID=<loc-id>           location details"
	@echo "  make oembed URL=<permalink>             embed HTML"
	@echo ""
	@echo "Insights:"
	@echo "  make insights ID=<thread-id> [METRIC=views,likes,...]"
	@echo "  make user-insights [METRIC=...] [SINCE=ts] [UNTIL=ts] [BREAKDOWN=country|...]"
	@echo ""
	@echo "Auth / token:"
	@echo "  make auth                         OAuth -> long-lived token"
	@echo "  make refresh                      refresh long-lived token (every <60d)"
	@echo "  make whoami                       GET /me + token expiry"
	@echo "  make debug-token                  inspect access token"
	@echo "  make env-check                    sanity-check .env"
	@echo ""
	@echo "Worker:"
	@echo "  make worker-deploy                redeploy Cloudflare worker"
	@echo "  make worker-tail                  tail worker logs"
	@echo ""
	@echo "Demo:"
	@echo "  make smoke                        full C/R/D smoke test"
	@echo "  make test                         run pytest suite (no network)"

# ---- Publish ----------------------------------------------------------------

post: ## text post; usage: make post MSG="hello" [LINK=...] [TOPIC=...] etc.
	@test -n "$(MSG)" || (echo "usage: make post MSG=\"hello\""; exit 2)
	$(RUN) threads.py post "$(MSG)" $(POST_FLAGS)

post-image: ## image post; usage: make post-image URL=<img> [MSG=...]
	@test -n "$(URL)" || (echo "usage: make post-image URL=<image-url> [MSG=...]"; exit 2)
	$(RUN) threads.py post "$(MSG)" --image-url "$(URL)" $(POST_FLAGS)

post-video: ## video post; usage: make post-video URL=<video> [MSG=...]
	@test -n "$(URL)" || (echo "usage: make post-video URL=<video-url> [MSG=...]"; exit 2)
	$(RUN) threads.py post "$(MSG)" --video-url "$(URL)" $(POST_FLAGS)

post-carousel: ## carousel post; usage: make post-carousel URLS="u1,u2" [MSG=...]
	@test -n "$(URLS)" || (echo "usage: make post-carousel URLS=\"u1,u2,...\" [MSG=...]"; exit 2)
	$(RUN) threads.py post "$(MSG)" --carousel-urls "$(URLS)" $(POST_FLAGS)

reply: ## reply to a thread; usage: make reply TO=<id> MSG="..."
	@test -n "$(TO)" -a -n "$(MSG)" || (echo "usage: make reply TO=<id> MSG=\"...\""; exit 2)
	$(RUN) threads.py post "$(MSG)" --reply-to "$(TO)" $(POST_FLAGS)

quote: ## quote a thread; usage: make quote OF=<id> MSG="..."
	@test -n "$(OF)" -a -n "$(MSG)" || (echo "usage: make quote OF=<id> MSG=\"...\""; exit 2)
	$(RUN) threads.py post "$(MSG)" --quote "$(OF)" $(POST_FLAGS)

# ---- Read mine --------------------------------------------------------------

list: ## list my threads
	$(RUN) threads.py list $(if $(LIMIT),$(LIMIT),10)

my-replies: ## list my replies
	$(RUN) threads.py my-replies $(if $(LIMIT),$(LIMIT),10)

mentions: ## list mentions of me
	$(RUN) threads.py mentions $(if $(LIMIT),$(LIMIT),10)

limits: ## quota / publishing limit
	$(RUN) threads.py limits

# ---- Read replies / conversations -------------------------------------------

replies: ## direct replies on a thread; usage: make replies ID=<thread-id>
	@test -n "$(ID)" || (echo "usage: make replies ID=<thread-id>"; exit 2)
	$(RUN) threads.py replies "$(ID)" $(if $(REVERSE),--reverse,)

conversation: ## conversation tree; usage: make conversation ID=<thread-id>
	@test -n "$(ID)" || (echo "usage: make conversation ID=<thread-id>"; exit 2)
	$(RUN) threads.py conversation "$(ID)" $(if $(REVERSE),--reverse,)

pending-replies: ## pending or ignored replies; usage: make pending-replies ID=<thread-id> [STATUS=pending|ignored]
	@test -n "$(ID)" || (echo "usage: make pending-replies ID=<thread-id> [STATUS=pending|ignored]"; exit 2)
	$(RUN) threads.py pending-replies "$(ID)" --status $(if $(STATUS),$(STATUS),pending)

# ---- Manage ----------------------------------------------------------------

hide: ## hide a reply; usage: make hide ID=<reply-id>
	@test -n "$(ID)" || (echo "usage: make hide ID=<reply-id>"; exit 2)
	$(RUN) threads.py manage-reply "$(ID)" --hide

unhide: ## unhide a reply; usage: make unhide ID=<reply-id>
	@test -n "$(ID)" || (echo "usage: make unhide ID=<reply-id>"; exit 2)
	$(RUN) threads.py manage-reply "$(ID)" --unhide

delete: ## delete a thread or reply
	@test -n "$(ID)" || (echo "usage: make delete ID=<thread_id>"; exit 2)
	$(RUN) threads.py delete "$(ID)"

# ---- Discover --------------------------------------------------------------

search: ## keyword search; usage: make search Q="..." [TYPE=TOP|RECENT]
	@test -n "$(Q)" || (echo "usage: make search Q=\"keyword\" [TYPE=TOP|RECENT]"; exit 2)
	$(RUN) threads.py search "$(Q)" --type $(if $(TYPE),$(TYPE),TOP)

location-search: ## location search; usage: make location-search Q="taipei"
	@test -n "$(Q)" || (echo "usage: make location-search Q=\"...\""; exit 2)
	$(RUN) threads.py location-search "$(Q)"

location-get: ## location details; usage: make location-get ID=<loc-id>
	@test -n "$(ID)" || (echo "usage: make location-get ID=<location-id>"; exit 2)
	$(RUN) threads.py location-get "$(ID)"

oembed: ## embed HTML for a permalink; usage: make oembed URL=<permalink>
	@test -n "$(URL)" || (echo "usage: make oembed URL=<thread-permalink>"; exit 2)
	$(RUN) threads.py oembed "$(URL)"

# ---- Insights --------------------------------------------------------------

insights: ## post-level insights; usage: make insights ID=<thread-id> [METRIC=...]
	@test -n "$(ID)" || (echo "usage: make insights ID=<thread-id> [METRIC=...]"; exit 2)
	$(RUN) threads.py insights "$(ID)" $(if $(METRIC),--metric "$(METRIC)",)

user-insights: ## user-level insights; usage: make user-insights [METRIC=...] [SINCE=ts] [UNTIL=ts] [BREAKDOWN=...]
	$(RUN) threads.py user-insights \
	  $(if $(METRIC),--metric "$(METRIC)",) \
	  $(if $(SINCE),--since "$(SINCE)",) \
	  $(if $(UNTIL),--until "$(UNTIL)",) \
	  $(if $(BREAKDOWN),--breakdown "$(BREAKDOWN)",)

# ---- Auth / token ----------------------------------------------------------

auth: ## OAuth flow -> long-lived token
	$(RUN) threads.py auth

refresh: ## refresh long-lived token (call every <60d)
	$(RUN) threads.py refresh

whoami: ## GET /me + token expiry
	$(RUN) threads.py whoami

debug-token: ## inspect access token
	$(RUN) threads.py debug-token

# ---- Demo / worker / env ---------------------------------------------------

smoke: ## full C/R/D smoke test
	$(RUN) threads.py smoke

test: ## run the pytest suite for threads_lib (no network)
	uv run --with pytest --with responses --with requests pytest tests/ -q

worker-deploy:
	cd cloudflare-worker && wrangler deploy

worker-tail:
	cd cloudflare-worker && wrangler tail

worker-delete:
	cd cloudflare-worker && wrangler delete

env-check: ## verify required keys are present in .env
	@test -f .env || (echo ".env missing -- copy .env.example and fill credentials"; exit 1)
	@for k in THREADS_CLIENT_ID THREADS_CLIENT_SECRET THREADS_REDIRECT_URI THREADS_WORKER_BASE; do \
		grep -q "^$$k=" .env || (echo "missing in .env: $$k"; exit 1); \
	done
	@echo ".env looks ok"
	@grep -q "^THREADS_ACCESS_TOKEN=" .env && echo "(has access token)" || echo "(no token -- run 'make auth')"
