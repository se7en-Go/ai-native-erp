"""Zero-network unit tests for the eight official social publishing adapters.

Every request is intercepted by `httpx.MockTransport`, so these tests assert the exact
vendor URLs, headers and payloads we would send in production — and, crucially, that
the decrypted OAuth token never appears in an error detail, an exception string, or a
persisted receipt.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import parse_qs

import httpx
import pytest

from erp_core.shared.errors import AppError
from erp_core.social.playbooks import PLAYBOOKS
from erp_core.social.publishers import (
    API_PLATFORMS,
    X_MAX_TEXT_LENGTH,
    HttpSocialPublisherDispatcher,
    SocialPublishReceipt,
    SocialPublishRequest,
    SocialTokenExpiredError,
    compose_social_text,
)

TOKEN = "tok-SUPER-SECRET-0123456789abcdef"
Handler = Callable[[httpx.Request], httpx.Response]


def _dispatcher(handler: Handler) -> HttpSocialPublisherDispatcher:
    return HttpSocialPublisherDispatcher(transport=httpx.MockTransport(handler))


def _request(platform: str, **overrides: object) -> SocialPublishRequest:
    payload: dict[str, object] = {
        "platform": platform,
        "external_account_id": "acct-1",
        "handle": "@brand",
        "access_token": TOKEN,
        "caption": "Spring collection",
        "hashtags": ["#spring", "handmade"],
        "media_urls": ["https://cdn.example.com/reel.mp4"],
        "account_config": {},
    }
    payload.update(overrides)
    return SocialPublishRequest.model_validate(payload)


def _form(request: httpx.Request) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


def _assert_scrubbed(error: AppError) -> None:
    assert TOKEN not in error.detail
    assert TOKEN not in str(error)
    assert "_" + TOKEN not in error.detail


def _assert_app_error(exc: pytest.ExceptionInfo[AppError], *, status: int, code: str) -> None:
    assert exc.value.status_code == status
    assert exc.value.code == code
    _assert_scrubbed(exc.value)


def test_api_platform_set_matches_the_locked_playbooks() -> None:
    locked = {item.platform for item in PLAYBOOKS if item.publish_mode == "api"}
    assert locked == API_PLATFORMS
    assert len(API_PLATFORMS) == 8


# --------------------------------------------------------------------------- compose


def test_compose_social_text_deduplicates_and_normalises_hashtags() -> None:
    text = compose_social_text(
        "  Spring haul  ", ["#spring", "Spring", "##", "", "#handmade", " handmade "]
    )
    assert text == "Spring haul #spring #handmade"


def test_compose_social_text_without_hashtags_keeps_caption_only() -> None:
    assert compose_social_text("Only copy", []) == "Only copy"
    assert compose_social_text("Only copy", ["   "]) == "Only copy"


def test_compose_social_text_truncates_safely_within_the_x_limit() -> None:
    caption = " ".join(["handmade spring collection"] * 20)
    text = compose_social_text(caption, ["#spring", "#handmade"], max_length=280)
    assert len(text) <= 280
    assert text.endswith("…")
    assert "#spring" not in text  # the hashtags had to go, not a half-cut tag
    assert "handmad…" not in text


def test_compose_social_text_truncation_never_exceeds_a_tiny_budget() -> None:
    assert compose_social_text("abcdef", [], max_length=1) == "…"
    assert compose_social_text("abcdef", [], max_length=0) == ""


# --------------------------------------------------------------------------- Instagram


async def test_instagram_publishes_a_reel_in_two_graph_steps() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "creation-42"})
        return httpx.Response(200, json={"id": "ig-post-42"})

    receipt = await _dispatcher(handler).publish(
        _request(
            "instagram",
            external_account_id="1789",
            caption="Spring haul",
            hashtags=["spring", "#Spring", "#handmade"],
            media_urls=["https://cdn.example.com/reel.mp4"],
        )
    )

    assert receipt.platform == "instagram"
    assert receipt.external_post_id == "ig-post-42"
    assert receipt.remote_status == "published"
    assert receipt.permalink is None  # Graph returns a media id, not a shortcode
    assert [request.url.path for request in calls] == [
        "/v21.0/1789/media",
        "/v21.0/1789/media_publish",
    ]
    assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in calls)
    assert json.loads(calls[0].content) == {
        "caption": "Spring haul #spring #handmade",
        "media_type": "REELS",
        "video_url": "https://cdn.example.com/reel.mp4",
    }
    assert json.loads(calls[1].content) == {"creation_id": "creation-42"}


async def test_instagram_uses_image_url_for_still_images() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "creation-7"})
        return httpx.Response(200, json={"id": "ig-post-7"})

    await _dispatcher(handler).publish(
        _request("instagram", media_urls=["https://cdn.example.com/look.JPG?w=1080"])
    )

    container = json.loads(calls[0].content)
    assert container["image_url"] == "https://cdn.example.com/look.JPG?w=1080"
    assert "video_url" not in container
    assert "media_type" not in container


async def test_instagram_prefers_account_config_when_external_id_is_absent() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "c-1"})
        return httpx.Response(200, json={"id": "p-1"})

    await _dispatcher(handler).publish(
        _request("instagram", external_account_id=None, account_config={"ig_user_id": "999"})
    )

    assert calls[0].url.path == "/v21.0/999/media"


# --------------------------------------------------------------------------- Facebook


async def test_facebook_page_feed_post_uses_message() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "9_101"})

    receipt = await _dispatcher(handler).publish(
        _request("facebook", external_account_id="page-9", media_urls=[], caption="Hello page")
    )

    assert receipt.external_post_id == "9_101"
    assert receipt.permalink == "https://www.facebook.com/9_101"
    assert calls[0].url.path == "/v21.0/page-9/feed"
    assert _form(calls[0]) == {"message": "Hello page #spring #handmade"}
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"


async def test_facebook_photo_post_uses_photo_edge_and_post_id() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "photo-1", "post_id": "9_202"})

    receipt = await _dispatcher(handler).publish(
        _request(
            "facebook",
            external_account_id="page-9",
            media_urls=["https://cdn.example.com/look.jpg"],
        )
    )

    assert receipt.external_post_id == "9_202"
    assert calls[0].url.path == "/v21.0/page-9/photos"
    assert _form(calls[0]) == {
        "url": "https://cdn.example.com/look.jpg",
        "caption": "Spring collection #spring #handmade",
    }


# --------------------------------------------------------------------------- TikTok


async def test_tiktok_pulls_the_video_from_url_and_defaults_to_self_only() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"data": {"publish_id": "tt-publish-1"}, "error": {"code": "ok"}}
        )

    receipt = await _dispatcher(handler).publish(
        _request(
            "tiktok",
            title="Factory floor tour",
            media_urls=["https://cdn.example.com/factory.mp4"],
        )
    )

    assert receipt.external_post_id == "tt-publish-1"
    assert receipt.remote_status == "uploading"
    assert receipt.permalink is None
    assert calls[0].url.path == "/v2/post/publish/video/init/"
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(calls[0].content) == {
        "post_info": {"title": "Factory floor tour", "privacy_level": "SELF_ONLY"},
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": "https://cdn.example.com/factory.mp4",
        },
    }


async def test_tiktok_honours_an_audited_privacy_level_override() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {"publish_id": "tt-2"}})

    await _dispatcher(handler).publish(
        _request("tiktok", account_config={"privacy_level": "PUBLIC_TO_EVERYONE"})
    )

    post_info = bodies[0]["post_info"]
    assert isinstance(post_info, dict)
    assert post_info["privacy_level"] == "PUBLIC_TO_EVERYONE"


# --------------------------------------------------------------------------- YouTube


async def test_youtube_data_api_uploads_snippet_and_status() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "yt-video-1", "kind": "youtube#video"})

    receipt = await _dispatcher(handler).publish(
        _request(
            "youtube",
            title="T" * 140,
            caption="Install walkthrough",
            hashtags=["#install", "#b2b"],
        )
    )

    assert receipt.external_post_id == "yt-video-1"
    assert receipt.permalink == "https://www.youtube.com/watch?v=yt-video-1"
    assert calls[0].url.path == "/youtube/v3/videos"
    assert calls[0].url.params["part"] == "snippet,status"
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    body = json.loads(calls[0].content)
    assert body["snippet"]["title"] == "T" * 100
    assert body["snippet"]["description"] == "Install walkthrough"
    # YouTube's `tags` field takes bare words, not "#" hashtags.
    assert body["snippet"]["tags"] == ["install", "b2b"]
    assert body["status"] == {"privacyStatus": "private"}


async def test_youtube_honours_the_configured_privacy_status() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "yt-2"})

    await _dispatcher(handler).publish(
        _request("youtube", account_config={"privacy_status": "unlisted"})
    )

    status = bodies[0]["status"]
    assert isinstance(status, dict)
    assert status["privacyStatus"] == "unlisted"


# --------------------------------------------------------------------------- LinkedIn


async def test_linkedin_rest_posts_uses_restli_header_and_urn_prefix() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:700"})

    receipt = await _dispatcher(handler).publish(_request("linkedin", external_account_id="123456"))

    assert receipt.external_post_id == "urn:li:share:700"
    assert receipt.permalink == "https://www.linkedin.com/feed/update/urn:li:share:700/"
    assert receipt.raw_response == {}
    assert calls[0].url.path == "/rest/posts"
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert calls[0].headers["x-restli-protocol-version"] == "2.0.0"
    assert calls[0].headers["linkedin-version"] == "202401"
    assert json.loads(calls[0].content) == {
        "author": "urn:li:organization:123456",
        "commentary": "Spring collection #spring #handmade",
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
    }


async def test_linkedin_keeps_an_explicit_urn_and_falls_back_to_json_id() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": "urn:li:share:701"})

    receipt = await _dispatcher(handler).publish(
        _request("linkedin", external_account_id="urn:li:person:abc")
    )

    assert receipt.external_post_id == "urn:li:share:701"
    author = json.loads(calls[0].content)["author"]
    assert author == "urn:li:person:abc"


# --------------------------------------------------------------------------- Pinterest


async def test_pinterest_v5_pin_uses_board_config_and_image_source() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"id": "pin-88"})

    receipt = await _dispatcher(handler).publish(
        _request(
            "pinterest",
            external_account_id=None,
            title="Spring look",
            media_urls=["https://cdn.example.com/pin.jpg"],
            account_config={"board_id": "board-77"},
        )
    )

    assert receipt.external_post_id == "pin-88"
    assert receipt.permalink == "https://www.pinterest.com/pin/pin-88/"
    assert calls[0].url.path == "/v5/pins"
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert json.loads(calls[0].content) == {
        "board_id": "board-77",
        "title": "Spring look",
        "description": "Spring collection #spring #handmade",
        "media_source": {"source_type": "image_url", "url": "https://cdn.example.com/pin.jpg"},
    }


# --------------------------------------------------------------------------- X


async def test_x_v2_tweet_caps_composed_text_at_280_characters() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json={"data": {"id": "1800000000000000001", "text": "ok"}})

    receipt = await _dispatcher(handler).publish(
        _request("x", caption="launch " * 60, hashtags=["#spring", "#spring", "#handmade"])
    )

    assert receipt.external_post_id == "1800000000000000001"
    assert receipt.permalink == "https://x.com/i/web/status/1800000000000000001"
    assert calls[0].url.path == "/2/tweets"
    assert calls[0].headers["authorization"] == f"Bearer {TOKEN}"
    text = json.loads(calls[0].content)["text"]
    assert len(text) <= X_MAX_TEXT_LENGTH
    assert text.endswith("…")
    assert text.startswith("launch launch")


async def test_receipts_scrub_an_upstream_token_echo() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201, json={"data": {"id": "1"}, "echo": TOKEN})

    receipt = await _dispatcher(handler).publish(_request("x"))
    assert isinstance(receipt, SocialPublishReceipt)
    assert TOKEN not in json.dumps(receipt.raw_response)
    assert "[redacted]" in json.dumps(receipt.raw_response)


# --------------------------------------------------------------------------- WeChat OA


async def test_wechat_oa_creates_a_draft_then_submits_freepublish() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/draft/add"):
            return httpx.Response(200, json={"media_id": "media-1"})
        return httpx.Response(200, json={"errcode": 0, "errmsg": "ok", "publish_id": "pub-1"})

    receipt = await _dispatcher(handler).publish(
        _request(
            "wechat_oa",
            title="春季新品",
            external_account_id="wx-account",
            account_config={"thumb_media_id": "thumb-9"},
        )
    )

    assert receipt.external_post_id == "pub-1"
    assert receipt.remote_status == "submitted"
    assert [request.url.path for request in calls] == [
        "/cgi-bin/draft/add",
        "/cgi-bin/freepublish/submit",
    ]
    assert [request.url.params["access_token"] for request in calls] == [TOKEN, TOKEN]
    assert json.loads(calls[0].content) == {
        "articles": [
            {
                "title": "春季新品",
                "content": "Spring collection #spring #handmade",
                "thumb_media_id": "thumb-9",
            }
        ]
    }
    assert json.loads(calls[1].content) == {"media_id": "media-1"}
    assert TOKEN not in json.dumps(receipt.raw_response)


# --------------------------------------------------------------------------- token expiry


async def test_instagram_401_raises_token_expired_without_leaking_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401, json={"error": {"code": 190, "message": f"token {TOKEN} has expired"}}
        )

    with pytest.raises(SocialTokenExpiredError) as exc:
        await _dispatcher(handler).publish(_request("instagram"))

    assert isinstance(exc.value, AppError)
    assert exc.value.status_code == 401
    assert exc.value.slug == "social-token-expired"
    _assert_scrubbed(exc.value)
    assert "[redacted]" in exc.value.detail


async def test_facebook_403_raises_token_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text=f"forbidden for {TOKEN}")

    with pytest.raises(SocialTokenExpiredError) as exc:
        await _dispatcher(handler).publish(_request("facebook", media_urls=[]))

    _assert_app_error(exc, status=401, code="ERR_SOCIAL_TOKEN_EXPIRED")


async def test_wechat_errcode_42001_raises_token_expired_without_the_url_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"errcode": 42001, "errmsg": f"access_token expired: {TOKEN}"}
        )

    with pytest.raises(SocialTokenExpiredError) as exc:
        await _dispatcher(handler).publish(_request("wechat_oa", title="春季新品"))

    _assert_app_error(exc, status=401, code="ERR_SOCIAL_TOKEN_EXPIRED")
    assert "42001" in exc.value.detail


async def test_wechat_non_token_errcode_is_a_502_publish_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errcode": 45009, "errmsg": "api freq out of limit"})

    with pytest.raises(AppError) as exc:
        await _dispatcher(handler).publish(_request("wechat_oa", title="春季新品"))

    _assert_app_error(exc, status=502, code="ERR_SOCIAL_PUBLISH_FAILED")
    assert "45009" in exc.value.detail


async def test_tiktok_access_token_error_code_raises_token_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": {
                    "code": "access_token_invalid",
                    "message": f"invalid token {TOKEN}",
                }
            },
        )

    with pytest.raises(SocialTokenExpiredError) as exc:
        await _dispatcher(handler).publish(_request("tiktok"))

    _assert_app_error(exc, status=401, code="ERR_SOCIAL_TOKEN_EXPIRED")


# --------------------------------------------------------------------------- HTTP errors


async def test_429_maps_to_rate_limited_and_scrubs_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": f"slow down, token={TOKEN}"})

    with pytest.raises(AppError) as exc:
        await _dispatcher(handler).publish(_request("x"))

    _assert_app_error(exc, status=429, code="ERR_SOCIAL_RATE_LIMITED")
    assert "[redacted]" in exc.value.detail


@pytest.mark.parametrize("status", [400, 404, 500, 503])
async def test_other_upstream_failures_map_to_502_and_scrub_the_token(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=f"upstream exploded with {TOKEN}")

    with pytest.raises(AppError) as exc:
        await _dispatcher(handler).publish(_request("youtube"))

    _assert_app_error(exc, status=502, code="ERR_SOCIAL_PUBLISH_FAILED")
    assert str(status) in exc.value.detail


async def test_network_error_is_a_502_that_never_echoes_the_wechat_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"could not reach {request.url}", request=request)

    with pytest.raises(AppError) as exc:
        await _dispatcher(handler).publish(_request("wechat_oa", title="春季新品"))

    _assert_app_error(exc, status=502, code="ERR_SOCIAL_PUBLISH_FAILED")
    assert "ConnectError" in exc.value.detail


# --------------------------------------------------------------------------- preconditions


@pytest.mark.parametrize("platform", ["instagram", "tiktok", "pinterest"])
async def test_media_backed_platforms_require_a_media_url(platform: str) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("no HTTP call may happen without media")

    config: dict[str, object] = {"board_id": "board-1"} if platform == "pinterest" else {}
    with pytest.raises(AppError) as exc:
        await _dispatcher(handler).publish(_request(platform, media_urls=[], account_config=config))

    _assert_app_error(exc, status=422, code="ERR_SOCIAL_MEDIA_REQUIRED")
    assert calls == []


@pytest.mark.parametrize(
    ("platform", "config"),
    [
        ("instagram", {}),
        ("facebook", {}),
        ("linkedin", {}),
        ("pinterest", {}),
    ],
)
async def test_missing_account_identifier_requires_config(
    platform: str, config: dict[str, object]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call may happen without an account id")

    with pytest.raises(AppError) as exc:
        await _dispatcher(handler).publish(
            _request(platform, external_account_id=None, account_config=config)
        )

    _assert_app_error(exc, status=422, code="ERR_SOCIAL_CONFIG_REQUIRED")


async def test_manual_export_platform_is_rejected_before_any_network_call() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("xiaohongshu has no publishing API")

    with pytest.raises(AppError) as exc:
        await _dispatcher(handler).publish(_request("xiaohongshu", media_urls=[]))

    _assert_app_error(exc, status=422, code="ERR_SOCIAL_MANUAL_PUBLISH_ONLY")
    assert "小红书" in exc.value.detail
    assert calls == []


async def test_non_json_upstream_body_is_a_scrubbed_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<html>{TOKEN}</html>")

    with pytest.raises(AppError) as exc:
        await _dispatcher(handler).publish(_request("x"))

    _assert_app_error(exc, status=502, code="ERR_SOCIAL_PUBLISH_FAILED")
    assert "non-JSON" in exc.value.detail
