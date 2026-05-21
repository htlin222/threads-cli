# /// script
# requires-python = ">=3.10"
# dependencies = ["requests>=2.32"]
# ///
"""Threads API CLI (thin dispatcher).

All helpers, validators, and typed errors live in ``threads_lib.py``.
This module only handles:

- argparse subcommands
- pretty-printing
- ``main()``: pre-flight token refresh + retry-once on AuthError

Subcommands: auth, refresh, whoami, debug-token,
post, list, my-replies, mentions, limits,
replies, conversation, pending-replies,
manage-reply, delete, search,
location-search, location-get, oembed,
insights, user-insights, smoke.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import webbrowser
from pathlib import Path

import requests

from threads_lib import (
    ApiError, AuthError, Config, ValidationError,
    DEFAULT_POST_INSIGHTS_METRICS, DEFAULT_USER_INSIGHTS_METRICS,
    GRAPH, REPLY_CONTROL_VALUES, SCOPES,
    build_auth_url, build_post_params,
    exchange_code_for_short_token, exchange_to_long_lived,
    fmt_expiry, gdelete, gget, gpost,
    infer_carousel_item_type,
    persist_token, poll_worker_for_code, refresh_long_lived,
    wait_for_container_ready,
)

ENV_PATH = Path(__file__).parent / ".env"


def pp(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# ---------- Auth + token ----------

def cmd_auth(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    client_id, redirect_uri, worker_base = cfg.require(
        "THREADS_CLIENT_ID", "THREADS_REDIRECT_URI", "THREADS_WORKER_BASE"
    )
    scopes = args.scopes or ",".join(SCOPES)
    state = secrets.token_urlsafe(32)
    url = build_auth_url(client_id, redirect_uri, scopes, state)
    print("\n=== AUTHORIZE ===")
    print("Opening this URL in your browser (approve to continue):\n")
    print(url, "\n")
    print(f"scopes requested: {scopes}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    code = poll_worker_for_code(worker_base, state)
    print("[ok] worker captured code, exchanging for short-lived token...")
    short = exchange_code_for_short_token(cfg, code)
    short_token = short["access_token"]
    user_id = str(short.get("user_id", ""))
    print(f"[ok] short-lived token acquired (user_id={user_id})")
    print("[..] exchanging short -> long-lived (60d)...")
    longl = exchange_to_long_lived(cfg, short_token)
    persist_token(cfg, longl["access_token"], longl.get("expires_in"), user_id)
    days = int(longl.get("expires_in", 0)) // 86400
    print(f"[ok] long-lived token saved to {ENV_PATH} (valid {days}d)")


def cmd_refresh(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    print(f"[..] current token: {fmt_expiry(cfg.get('THREADS_TOKEN_EXPIRES_AT'))}")
    out = refresh_long_lived(cfg, token)
    persist_token(cfg, out["access_token"], out.get("expires_in"), cfg.get("THREADS_USER_ID"))
    cfg = Config.load(ENV_PATH)
    print(f"[ok] new token: {fmt_expiry(cfg.get('THREADS_TOKEN_EXPIRES_AT'))}")


def cmd_whoami(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    me = gget(
        "/me", token,
        fields="id,username,threads_profile_picture_url,threads_biography",
    )
    pp(me)
    print(f"\ntoken: {fmt_expiry(cfg.get('THREADS_TOKEN_EXPIRES_AT'))}")


def cmd_debug_token(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    r = requests.get(
        f"{GRAPH}/debug_token",
        params={"input_token": token, "access_token": token},
        timeout=30,
    )
    if r.status_code != 200:
        raise ApiError(status=r.status_code, code=None, subcode=None, message=r.text)
    pp(r.json())


# ---------- Publishing ----------

def _publish_and_read(user_id: str, container_id: str, token: str) -> dict:
    pub = gpost(f"/{user_id}/threads_publish", token, creation_id=container_id)
    tid = pub["id"]
    return gget(f"/{tid}", token, fields="id,text,permalink,timestamp,media_type")


def cmd_post(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token, user_id = cfg.require("THREADS_ACCESS_TOKEN", "THREADS_USER_ID")

    params = build_post_params(
        text=args.text or "",
        link=args.link, topic=args.topic, alt_text=args.alt_text,
        reply_to=args.reply_to, quote=args.quote,
        reply_control=args.reply_control,
        countries=args.countries, location=args.location,
    )

    on_status = lambda cid, st: print(f"[..] container {cid} status={st}")

    if args.carousel_urls:
        urls = [u.strip() for u in args.carousel_urls.split(",") if u.strip()]
        if not (2 <= len(urls) <= 20):
            raise ValidationError(f"carousel needs 2-20 items, got {len(urls)}")
        children: list[str] = []
        for u in urls:
            mtype = infer_carousel_item_type(u)
            cp: dict = {"media_type": mtype, "is_carousel_item": "true"}
            cp["image_url" if mtype == "IMAGE" else "video_url"] = u
            print(f"[..] creating carousel child ({mtype}): {u}")
            child = gpost(f"/{user_id}/threads", token, **cp)
            wait_for_container_ready(child["id"], token, on_status=on_status)
            children.append(child["id"])
        params["media_type"] = "CAROUSEL"
        params["children"] = ",".join(children)
        print(f"[..] creating CAROUSEL parent with {len(children)} children")
        container = gpost(f"/{user_id}/threads", token, **params)
    elif args.image_url:
        params["media_type"] = "IMAGE"
        params["image_url"] = args.image_url
        print("[..] creating IMAGE container")
        container = gpost(f"/{user_id}/threads", token, **params)
    elif args.video_url:
        params["media_type"] = "VIDEO"
        params["video_url"] = args.video_url
        print("[..] creating VIDEO container")
        container = gpost(f"/{user_id}/threads", token, **params)
        wait_for_container_ready(container["id"], token, on_status=on_status)
    else:
        params["media_type"] = "TEXT"
        print("[..] creating TEXT container")
        container = gpost(f"/{user_id}/threads", token, **params)
        time.sleep(2)

    cid = container["id"]
    print(f"[..] publishing {cid}")
    pp(_publish_and_read(user_id, cid, token))


def cmd_smoke(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token, user_id = cfg.require("THREADS_ACCESS_TOKEN", "THREADS_USER_ID")
    text = f"Threads API smoke test @ {int(time.time())} -- safe to delete"
    print("=== CREATE ===")
    container = gpost(f"/{user_id}/threads", token, media_type="TEXT", text=text)
    cid = container["id"]
    time.sleep(2)
    published = gpost(f"/{user_id}/threads_publish", token, creation_id=cid)
    tid = published["id"]
    print(f"thread_id={tid}")
    print("\n=== READ ===")
    pp(gget(f"/{tid}", token, fields="id,text,permalink,timestamp,media_type"))
    print("\n=== DELETE ===")
    pp(gdelete(f"/{tid}", token))
    r = requests.get(
        f"{GRAPH}/v1.0/{tid}",
        params={"access_token": token, "fields": "id"},
        timeout=30,
    )
    print(f"verify: GET after delete -> HTTP {r.status_code} (expected != 200)")


# ---------- Read mine ----------

def _print_thread_list(data: dict) -> None:
    for t in data.get("data", []):
        ts = t.get("timestamp", "")
        print(f"{t['id']}  {ts}  {t.get('media_type','?')}")
        text = (t.get("text") or "").replace("\n", " ")
        if text:
            print(f"  > {text[:120]}{'...' if len(text) > 120 else ''}")
        if t.get("permalink"):
            print(f"  {t['permalink']}")
    if data.get("paging", {}).get("next"):
        print("\n(more available; paging.next cursor present)")


def cmd_list(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token, user_id = cfg.require("THREADS_ACCESS_TOKEN", "THREADS_USER_ID")
    _print_thread_list(gget(
        f"/{user_id}/threads", token,
        fields="id,text,permalink,timestamp,media_type",
        limit=args.limit,
    ))


def cmd_my_replies(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token, user_id = cfg.require("THREADS_ACCESS_TOKEN", "THREADS_USER_ID")
    _print_thread_list(gget(
        f"/{user_id}/replies", token,
        fields="id,text,permalink,timestamp,media_type,root_post,replied_to",
        limit=args.limit,
    ))


def cmd_mentions(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token, user_id = cfg.require("THREADS_ACCESS_TOKEN", "THREADS_USER_ID")
    _print_thread_list(gget(
        f"/{user_id}/mentions", token,
        fields="id,text,permalink,timestamp,username,media_type",
        limit=args.limit,
    ))


def cmd_limits(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token, user_id = cfg.require("THREADS_ACCESS_TOKEN", "THREADS_USER_ID")
    pp(gget(
        f"/{user_id}/threads_publishing_limit", token,
        fields="quota_usage,config,reply_quota_usage,reply_config",
    ))


# ---------- Read replies / conversations ----------

def cmd_replies(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    try:
        data = gget(
            f"/{args.id}/replies", token,
            fields="id,text,permalink,timestamp,username,media_type,hide_status",
            reverse=str(args.reverse).lower(),
        )
    except ApiError as e:
        if e.code != 100 or "dev mode" not in (e.message or "").lower():
            raise
        print(
            "note: /replies gated in dev mode, falling back to /conversation",
            file=sys.stderr,
        )
        conv = gget(
            f"/{args.id}/conversation", token,
            fields="id,text,permalink,timestamp,username,media_type,"
                   "hide_status,replied_to",
            reverse=str(args.reverse).lower(),
        )
        data = {
            "data": [
                t for t in conv.get("data", [])
                if (t.get("replied_to") or {}).get("id") == args.id
            ],
            "paging": conv.get("paging", {}),
        }
    _print_thread_list(data)


def cmd_conversation(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    _print_thread_list(gget(
        f"/{args.id}/conversation", token,
        fields="id,text,permalink,timestamp,username,media_type,hide_status,replied_to,root_post",
        reverse=str(args.reverse).lower(),
    ))


def cmd_pending_replies(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    _print_thread_list(gget(
        f"/{args.id}/pending_replies", token,
        fields="id,text,permalink,timestamp,username,reply_approval_status",
        approval_status=args.status,
    ))


# ---------- Manage ----------

def cmd_manage_reply(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    pp(gpost(f"/{args.id}/manage_reply", token, hide=str(args.hide).lower()))


def cmd_delete(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    pp(gdelete(f"/{args.id}", token))


# ---------- Discovery ----------

def cmd_search(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    _print_thread_list(gget(
        "/keyword_search", token,
        q=args.query, search_type=args.type,
        fields="id,text,permalink,timestamp,username,media_type",
    ))


def cmd_location_search(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    pp(gget(
        "/location_search", token,
        q=args.query,
        fields="id,name,address,city,country,latitude,longitude",
    ))


def cmd_location_get(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    pp(gget(
        f"/{args.id}", token,
        fields="id,name,address,city,country,latitude,longitude",
    ))


def cmd_oembed(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    r = requests.get(
        f"{GRAPH}/oembed",
        params={"url": args.url, "access_token": token},
        timeout=30,
    )
    if r.status_code != 200:
        text = r.text
        if '"code":10' in text or "permission" in text.lower():
            raise ApiError(
                status=r.status_code, code=10, subcode=None,
                message=("oembed is gated behind Meta App Review. "
                         "Submit the app under 'oEmbed Read' to enable. "
                         f"Raw: {text}"),
            )
        raise ApiError(status=r.status_code, code=None, subcode=None, message=text)
    pp(r.json())


# ---------- Insights ----------

def cmd_insights(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token = cfg.require("THREADS_ACCESS_TOKEN")[0]
    pp(gget(
        f"/{args.id}/insights", token,
        metric=args.metric or DEFAULT_POST_INSIGHTS_METRICS,
    ))


def cmd_user_insights(args: argparse.Namespace) -> None:
    cfg = Config.load(ENV_PATH)
    token, user_id = cfg.require("THREADS_ACCESS_TOKEN", "THREADS_USER_ID")
    params: dict = {"metric": args.metric or DEFAULT_USER_INSIGHTS_METRICS}
    if args.since:
        params["since"] = args.since
    if args.until:
        params["until"] = args.until
    if args.breakdown:
        params["breakdown"] = args.breakdown
    pp(gget(f"/{user_id}/threads_insights", token, **params))


# ---------- argparse ----------

def _add_post_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("text", nargs="?", default="", help="thread text (omit if media-only)")
    p.add_argument("--image-url")
    p.add_argument("--video-url")
    p.add_argument("--carousel-urls", help="comma-separated 2-20 image/video URLs")
    p.add_argument("--alt-text")
    p.add_argument("--reply-to")
    p.add_argument("--quote")
    p.add_argument("--link")
    p.add_argument("--topic")
    p.add_argument("--reply-control", choices=sorted(REPLY_CONTROL_VALUES))
    p.add_argument("--countries", help="comma-separated ISO country codes")
    p.add_argument("--location")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="threads", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="OAuth flow -> long-lived token")
    a.add_argument("--scopes", help=f"comma list; default: {','.join(SCOPES)}")
    a.set_defaults(func=cmd_auth)

    sub.add_parser("refresh").set_defaults(func=cmd_refresh)
    sub.add_parser("whoami").set_defaults(func=cmd_whoami)
    sub.add_parser("debug-token").set_defaults(func=cmd_debug_token)

    post = sub.add_parser("post")
    _add_post_flags(post)
    post.set_defaults(func=cmd_post)

    lst = sub.add_parser("list")
    lst.add_argument("limit", nargs="?", type=int, default=10)
    lst.set_defaults(func=cmd_list)

    mr = sub.add_parser("my-replies")
    mr.add_argument("limit", nargs="?", type=int, default=10)
    mr.set_defaults(func=cmd_my_replies)

    mn = sub.add_parser("mentions")
    mn.add_argument("limit", nargs="?", type=int, default=10)
    mn.set_defaults(func=cmd_mentions)

    sub.add_parser("limits").set_defaults(func=cmd_limits)

    rep = sub.add_parser("replies")
    rep.add_argument("id"); rep.add_argument("--reverse", action="store_true")
    rep.set_defaults(func=cmd_replies)

    conv = sub.add_parser("conversation")
    conv.add_argument("id"); conv.add_argument("--reverse", action="store_true")
    conv.set_defaults(func=cmd_conversation)

    pr = sub.add_parser("pending-replies")
    pr.add_argument("id")
    pr.add_argument("--status", choices=["pending", "ignored"], default="pending")
    pr.set_defaults(func=cmd_pending_replies)

    mg = sub.add_parser("manage-reply")
    mg.add_argument("id")
    g = mg.add_mutually_exclusive_group(required=True)
    g.add_argument("--hide", dest="hide", action="store_true")
    g.add_argument("--unhide", dest="hide", action="store_false")
    mg.set_defaults(func=cmd_manage_reply)

    dl = sub.add_parser("delete")
    dl.add_argument("id")
    dl.set_defaults(func=cmd_delete)

    s = sub.add_parser("search")
    s.add_argument("query"); s.add_argument("--type", choices=["TOP", "RECENT"], default="TOP")
    s.set_defaults(func=cmd_search)

    ls = sub.add_parser("location-search")
    ls.add_argument("query"); ls.set_defaults(func=cmd_location_search)

    lg = sub.add_parser("location-get")
    lg.add_argument("id"); lg.set_defaults(func=cmd_location_get)

    oe = sub.add_parser("oembed")
    oe.add_argument("url"); oe.set_defaults(func=cmd_oembed)

    ins = sub.add_parser("insights")
    ins.add_argument("id"); ins.add_argument("--metric")
    ins.set_defaults(func=cmd_insights)

    ui = sub.add_parser("user-insights")
    ui.add_argument("--metric"); ui.add_argument("--since"); ui.add_argument("--until")
    ui.add_argument("--breakdown")
    ui.set_defaults(func=cmd_user_insights)

    sub.add_parser("smoke").set_defaults(func=cmd_smoke)
    return p


# ---------- main + auto-recovery ----------

def _try_proactive_refresh(cfg: Config, args: argparse.Namespace) -> None:
    """If the token is within 5 min of expiry, refresh BEFORE running the command."""
    if args.cmd in ("auth", "refresh"):
        return
    expires_at = cfg.get("THREADS_TOKEN_EXPIRES_AT")
    if not expires_at:
        return
    try:
        remaining = int(expires_at) - int(time.time())
    except ValueError:
        return
    if 0 < remaining < 300:
        token = cfg.get("THREADS_ACCESS_TOKEN")
        if not token:
            return
        print(f"[!] token expires in {remaining}s; pre-refreshing...", file=sys.stderr)
        try:
            out = refresh_long_lived(cfg, token)
            persist_token(cfg, out["access_token"], out.get("expires_in"), cfg.get("THREADS_USER_ID"))
            print("[ok] pre-refresh done", file=sys.stderr)
        except ApiError as e:
            print(f"[!] pre-refresh failed (continuing): {e}", file=sys.stderr)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    cfg = Config.load(ENV_PATH)
    _try_proactive_refresh(cfg, args)

    try:
        args.func(args)
    except AuthError as e:
        if args.cmd in ("auth", "refresh"):
            print(f"ERROR: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"[!] auth error: {e}", file=sys.stderr)
        cfg = Config.load(ENV_PATH)
        token = cfg.get("THREADS_ACCESS_TOKEN")
        if not token:
            print("    no token in .env; run: make auth", file=sys.stderr)
            sys.exit(1)
        try:
            print("[..] attempting refresh + retry...", file=sys.stderr)
            out = refresh_long_lived(cfg, token)
            persist_token(cfg, out["access_token"], out.get("expires_in"), cfg.get("THREADS_USER_ID"))
            print("[ok] refreshed; retrying...", file=sys.stderr)
            args.func(args)
        except ApiError as refresh_err:
            print(f"[!] refresh also failed: {refresh_err}", file=sys.stderr)
            print("    token may be >60d expired; run: make auth", file=sys.stderr)
            sys.exit(1)
    except ValidationError as e:
        print(f"INPUT ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except ApiError as e:
        print(f"API ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
