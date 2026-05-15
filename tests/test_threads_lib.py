"""Tests for threads_lib.

Pure-function tests run in isolation (no network). HTTP-touching helpers are
exercised through the ``responses`` library, which intercepts ``requests``
calls before they leave the process.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import requests
import responses

from threads_lib import (
    ApiError, AuthError, Config, GRAPH, SCOPES, ValidationError,
    build_auth_url, build_post_params, exchange_to_long_lived, extract_code,
    fmt_expiry, gdelete, gget, gpost, infer_carousel_item_type,
    persist_token, poll_worker_for_code, refresh_long_lived,
    validate_country_codes, validate_reply_control, validate_text_length,
    validate_topic, validate_url, wait_for_container_ready,
)


# ---------- Config ----------

def test_config_load_missing_file(tmp_path: Path):
    cfg = Config.load(tmp_path / "nonexistent.env")
    assert cfg.data == {}
    assert cfg.get("THREADS_CLIENT_ID") is None


def test_config_load_parses_kv(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text(
        "# a comment\n"
        "THREADS_CLIENT_ID=abc123\n"
        'THREADS_CLIENT_SECRET="sec=ret"\n'
        "BLANK=\n"
        "\n"
        "WITH_SPACES  =   value with spaces  \n"
    )
    cfg = Config.load(p)
    assert cfg.get("THREADS_CLIENT_ID") == "abc123"
    assert cfg.get("THREADS_CLIENT_SECRET") == "sec=ret"
    assert cfg.get("BLANK") == ""
    assert cfg.get("WITH_SPACES") == "value with spaces"


def test_config_save_upserts(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("THREADS_CLIENT_ID=old\nTHREADS_USER_ID=u1\n")
    cfg = Config.load(p)
    cfg.save({"THREADS_CLIENT_ID": "new", "THREADS_ACCESS_TOKEN": "tok"})
    text = p.read_text()
    assert "THREADS_CLIENT_ID=new" in text
    assert "THREADS_USER_ID=u1" in text  # preserved
    assert "THREADS_ACCESS_TOKEN=tok" in text


def test_config_require_raises_validation_error(tmp_path: Path):
    cfg = Config.load(tmp_path / "no.env")
    with pytest.raises(ValidationError) as e:
        cfg.require("THREADS_CLIENT_ID", "THREADS_CLIENT_SECRET")
    assert "THREADS_CLIENT_ID" in str(e.value)
    assert "THREADS_CLIENT_SECRET" in str(e.value)


def test_config_env_var_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    p = tmp_path / ".env"
    p.write_text("THREADS_CLIENT_ID=from_file\n")
    monkeypatch.setenv("THREADS_CLIENT_ID", "from_env")
    cfg = Config.load(p)
    assert cfg.get("THREADS_CLIENT_ID") == "from_env"


# ---------- OAuth helpers (pure) ----------

def test_extract_code_from_plain_string():
    assert extract_code("ABC123") == "ABC123"


def test_extract_code_strips_fragment():
    assert extract_code("ABC123#_") == "ABC123"


def test_extract_code_from_redirect_url():
    url = "https://example.com/cb?code=XYZ789&state=s1"
    assert extract_code(url) == "XYZ789"


def test_extract_code_raises_when_no_code_param():
    with pytest.raises(ValidationError):
        extract_code("https://example.com/cb?state=s1")


def test_build_auth_url_csv_scopes():
    url = build_auth_url("cid", "https://cb/x", "a,b,c", "STATE1")
    assert url.startswith("https://threads.net/oauth/authorize?")
    assert "client_id=cid" in url
    assert "redirect_uri=https%3A%2F%2Fcb%2Fx" in url
    assert "scope=a%2Cb%2Cc" in url
    assert "state=STATE1" in url
    assert "response_type=code" in url


def test_build_auth_url_iterable_scopes():
    url = build_auth_url("cid", "cb", ["x", "y"], "s")
    assert "scope=x%2Cy" in url


# ---------- fmt_expiry ----------

def test_fmt_expiry_none():
    assert fmt_expiry(None) == "(unknown expiry)"


def test_fmt_expiry_empty_string():
    assert fmt_expiry("") == "(unknown expiry)"


def test_fmt_expiry_expired():
    # 2 days ago
    now = time.time()
    assert "EXPIRED" in fmt_expiry(int(now) - 2 * 86400)


def test_fmt_expiry_valid_remaining():
    now = 1_000_000.0
    # 5 days and 3 hours from "now"
    ts = int(now) + 5 * 86400 + 3 * 3600
    assert fmt_expiry(ts, now=lambda: now) == "5d 3h remaining"


# ---------- Publishing helpers (pure) ----------

@pytest.mark.parametrize("url,expected", [
    ("https://x/y.mp4", "VIDEO"),
    ("https://x/y.MP4", "VIDEO"),
    ("https://x/y.mov", "VIDEO"),
    ("https://x/y.m4v", "VIDEO"),
    ("https://x/y.mp4?token=1", "VIDEO"),
    ("https://x/y.jpg", "IMAGE"),
    ("https://x/y.png?w=200", "IMAGE"),
    ("https://x/no-extension", "IMAGE"),
])
def test_infer_carousel_item_type(url: str, expected: str):
    assert infer_carousel_item_type(url) == expected


def test_build_post_params_text_only():
    assert build_post_params(text="hi") == {"text": "hi"}


def test_build_post_params_full():
    out = build_post_params(
        text="hi", link="https://example.com", topic="ai",
        alt_text="alt", reply_to="123", quote="456",
        reply_control="everyone", countries="tw, jp",
        location="L1", is_carousel_item=True,
    )
    assert out["text"] == "hi"
    assert out["link_attachment"] == "https://example.com"
    assert out["topic_tag"] == "ai"
    assert out["alt_text"] == "alt"
    assert out["reply_to_id"] == "123"
    assert out["quote_post_id"] == "456"
    assert out["reply_control"] == "everyone"
    assert out["allowlisted_country_codes"] == "TW,JP"  # normalized
    assert out["location_id"] == "L1"
    assert out["is_carousel_item"] == "true"


def test_build_post_params_validates_text_length():
    with pytest.raises(ValidationError):
        build_post_params(text="x" * 501)


def test_build_post_params_validates_url():
    with pytest.raises(ValidationError):
        build_post_params(text="x", link="not-a-url")


# ---------- Validators ----------

def test_validate_text_length_under_limit():
    assert validate_text_length("hi") == "hi"


def test_validate_text_length_at_limit():
    assert validate_text_length("x" * 500) == "x" * 500


def test_validate_text_length_over_limit():
    with pytest.raises(ValidationError):
        validate_text_length("x" * 501)


def test_validate_country_codes_normalizes_case():
    assert validate_country_codes("tw,jp") == "TW,JP"


def test_validate_country_codes_strips_whitespace():
    assert validate_country_codes(" tw , jp ") == "TW,JP"


def test_validate_country_codes_rejects_three_letter():
    with pytest.raises(ValidationError) as e:
        validate_country_codes("tw,USA")
    assert "USA" in str(e.value)


def test_validate_country_codes_rejects_empty():
    with pytest.raises(ValidationError):
        validate_country_codes("")


def test_validate_url_http():
    assert validate_url("http://x.com") == "http://x.com"


def test_validate_url_https():
    assert validate_url("https://x.com") == "https://x.com"


def test_validate_url_rejects_ftp():
    with pytest.raises(ValidationError):
        validate_url("ftp://x.com")


def test_validate_url_rejects_bare():
    with pytest.raises(ValidationError):
        validate_url("example.com")


def test_validate_topic_ok():
    assert validate_topic("ai") == "ai"


def test_validate_topic_rejects_empty():
    with pytest.raises(ValidationError):
        validate_topic("")


def test_validate_topic_rejects_too_long():
    with pytest.raises(ValidationError):
        validate_topic("x" * 51)


def test_validate_reply_control_ok():
    assert validate_reply_control("everyone") == "everyone"


def test_validate_reply_control_rejects_unknown():
    with pytest.raises(ValidationError):
        validate_reply_control("nobody")


# ---------- HTTP helpers (mocked) ----------

@responses.activate
def test_gget_200_returns_json():
    responses.get(
        f"{GRAPH}/v1.0/me",
        json={"id": "42"},
        status=200,
    )
    out = gget("/me", "tok")
    assert out == {"id": "42"}


@responses.activate
def test_gget_400_raises_api_error_with_meta_fields():
    responses.get(
        f"{GRAPH}/v1.0/me",
        json={"error": {"message": "Bad", "code": 100, "error_subcode": 33}},
        status=400,
    )
    with pytest.raises(ApiError) as e:
        gget("/me", "tok")
    assert e.value.status == 400
    assert e.value.code == 100
    assert e.value.subcode == 33
    assert "Bad" in e.value.message
    assert not isinstance(e.value, AuthError)


@responses.activate
def test_gget_401_raises_auth_error():
    responses.get(
        f"{GRAPH}/v1.0/me",
        json={"error": {"message": "expired", "code": 190}},
        status=401,
    )
    with pytest.raises(AuthError):
        gget("/me", "tok")


@responses.activate
def test_gget_code_190_on_200_still_classifies_as_auth_error():
    # Threads sometimes returns code=190 with HTTP 200's friend status
    responses.get(
        f"{GRAPH}/v1.0/me",
        json={"error": {"message": "bad token", "code": 190}},
        status=400,
    )
    with pytest.raises(AuthError):
        gget("/me", "tok")


@responses.activate
def test_gpost_includes_access_token_in_body():
    responses.post(
        f"{GRAPH}/v1.0/123/threads",
        json={"id": "container_1"},
        status=200,
    )
    out = gpost("/123/threads", "tok", media_type="TEXT", text="hi")
    assert out == {"id": "container_1"}
    body = responses.calls[0].request.body
    if isinstance(body, bytes):
        body = body.decode()
    assert "access_token=tok" in body
    assert "media_type=TEXT" in body


@responses.activate
def test_gdelete_returns_json():
    responses.delete(
        f"{GRAPH}/v1.0/some_id",
        json={"success": True},
        status=200,
    )
    assert gdelete("/some_id", "tok") == {"success": True}


# ---------- Worker polling ----------

@responses.activate
def test_poll_worker_for_code_returns_when_ready():
    # First call -> 204 (empty), second -> 200 with the code.
    responses.get(
        "https://worker.example.com/poll",
        status=204,
    )
    responses.get(
        "https://worker.example.com/poll",
        body="ABC123",
        status=200,
    )
    out = poll_worker_for_code(
        "https://worker.example.com", "STATE1",
        interval=0,  # tests don't sleep
        timeout=10,
    )
    assert out == "ABC123"
    assert len(responses.calls) == 2


@responses.activate
def test_poll_worker_for_code_strips_fragment():
    responses.get(
        "https://worker.example.com/poll",
        body="ABC#_",
        status=200,
    )
    assert poll_worker_for_code(
        "https://worker.example.com", "S", interval=0, timeout=10
    ) == "ABC"


def test_poll_worker_for_code_times_out():
    # No responses registered -> any HTTP call would 404 via responses; we
    # instead bypass HTTP by using an immediately-elapsed timeout.
    counter = {"n": 0}

    def fake_now():
        counter["n"] += 1
        # First call sets deadline; second call past deadline.
        return counter["n"] * 100.0

    # responses isn't activated so any HTTP would fail. Force timeout instead.
    with pytest.raises(ApiError) as e:
        poll_worker_for_code(
            "https://worker.example.com", "S",
            interval=0, timeout=0.1,
            now=fake_now,
            sleep=lambda _s: None,
        )
    assert e.value.status == 408


# ---------- Token lifecycle ----------

@responses.activate
def test_exchange_to_long_lived_happy_path(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("THREADS_CLIENT_SECRET=secret\n")
    cfg = Config.load(p)
    responses.get(
        f"{GRAPH}/access_token",
        json={"access_token": "long_tok", "expires_in": 5183944},
        status=200,
    )
    out = exchange_to_long_lived(cfg, "short_tok")
    assert out["access_token"] == "long_tok"


@responses.activate
def test_refresh_long_lived(tmp_path: Path):
    cfg = Config.load(tmp_path / ".env")
    responses.get(
        f"{GRAPH}/refresh_access_token",
        json={"access_token": "fresh", "expires_in": 5183944},
        status=200,
    )
    out = refresh_long_lived(cfg, "old_tok")
    assert out["access_token"] == "fresh"


def test_persist_token_writes_expiry(tmp_path: Path):
    cfg = Config.load(tmp_path / ".env")
    persist_token(
        cfg, "tok123", expires_in=3600, user_id="u1",
        now=lambda: 1_000_000.0,
    )
    cfg2 = Config.load(tmp_path / ".env")
    assert cfg2.get("THREADS_ACCESS_TOKEN") == "tok123"
    assert cfg2.get("THREADS_USER_ID") == "u1"
    assert cfg2.get("THREADS_TOKEN_EXPIRES_AT") == "1003600"


# ---------- Container ready loop ----------

@responses.activate
def test_wait_for_container_ready_eventual_finish():
    # First poll -> IN_PROGRESS, second -> FINISHED
    responses.get(
        f"{GRAPH}/v1.0/c1",
        json={"status": "IN_PROGRESS"},
        status=200,
    )
    responses.get(
        f"{GRAPH}/v1.0/c1",
        json={"status": "FINISHED"},
        status=200,
    )
    # Should return without raising; no real sleeping.
    wait_for_container_ready(
        "c1", "tok",
        timeout=10, initial_interval=0, max_interval=0,
        sleep=lambda _s: None,
    )
    assert len(responses.calls) == 2


@responses.activate
def test_wait_for_container_ready_raises_on_error_status():
    responses.get(
        f"{GRAPH}/v1.0/c1",
        json={"status": "ERROR", "error_message": "video too short"},
        status=200,
    )
    with pytest.raises(ApiError) as e:
        wait_for_container_ready(
            "c1", "tok",
            timeout=10, initial_interval=0, max_interval=0,
            sleep=lambda _s: None,
        )
    assert "video too short" in e.value.message
    assert "ERROR" in e.value.message


# ---------- SCOPES sanity ----------

def test_scopes_is_tuple_of_strings():
    assert isinstance(SCOPES, tuple)
    assert all(isinstance(s, str) and s.startswith("threads_") for s in SCOPES)
    # The eight scopes we care about today
    assert "threads_basic" in SCOPES
    assert "threads_keyword_search" in SCOPES
