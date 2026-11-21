"""Official HTTP publishing adapters for the eight `publish_mode == "api"` platforms.

`erp_core.social` is isolated from the commerce domains (`infra/importlinter.ini`
contract `social_isolated`), so this module owns only the network contract: it
receives an already-decrypted OAuth token plus an already-validated post, calls the
official vendor API, and returns a receipt. Retry policy, account status changes and
persistence stay in `SocialService`.

Two invariants are load-bearing:

* an access token never appears in an `AppError.detail`, a log line or a receipt —
  every outbound failure detail passes through :func:`_scrub_secret`;
* a revoked authorization is a typed :class:`SocialTokenExpiredError` (HTTP 401,
  `ERR_SOCIAL_TOKEN_EXPIRED`) rather than a generic upstream failure, so the caller
  can flip `social_accounts.status = "token_expired"` instead of retrying forever.

The playbooks file is the compliance source of truth: `manual_export` platforms
(小红书, 抖音, 快手, B站, 视频号, 微博) have no general publishing API, so scheduling
them through this dispatcher is refused with `ERR_SOCIAL_MANUAL_PUBLISH_ONLY`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field

from erp_core.shared.errors import AppError
from erp_core.social.playbooks import SocialPlatform, playbook_for

GRAPH_API_BASE = "https://graph.facebook.com/v21.0"
TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"
LINKEDIN_API_BASE = "https://api.linkedin.com"
PINTEREST_API_BASE = "https://api.pinterest.com"
X_API_BASE = "https://api.x.com"
WECHAT_OA_API_BASE = "https://api.weixin.qq.com/cgi-bin"

# The locked subset of `PLAYBOOKS` whose `publish_mode` is `api`.
API_PLATFORMS: frozenset[SocialPlatform] = frozenset(
    {
        "instagram",
        "facebook",
        "tiktok",
        "youtube",
        "linkedin",
        "pinterest",
        "x",
        "wechat_oa",
    }
)

VIDEO_SUFFIXES: tuple[str, ...] = (".mp4", ".mov")
X_MAX_TEXT_LENGTH = 280
YOUTUBE_MAX_TITLE_LENGTH = 100
TIKTOK_MAX_TITLE_LENGTH = 2200
WECHAT_MAX_TITLE_LENGTH = 64
REDACTED = "[redacted]"
ELLIPSIS = "…"

# WeChat `errcode` values that mean "the OAuth credential is dead", not "retry later".
WECHAT_TOKEN_ERRCODES: frozenset[int] = frozenset({40001, 40014, 42001})
_TIKTOK_TOKEN_ERROR_CODES: frozenset[str] = frozenset(
    {"access_token_invalid", "token_expired", "invalid_token", "unauthorized"}
)
_HASHTAG_WHITESPACE = re.compile(r"\s+")


class SocialTokenExpiredError(AppError):
    """Upstream rejected the OAuth credential: the account must be re-authorized.

    Raised for HTTP 401/403 and for vendor-specific token error codes (WeChat
    `40001`/`42001`, TikTok `access_token_invalid`/`token_expired`). The caller marks
    the `social_accounts` row `status = "token_expired"` instead of retrying.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(
            slug="social-token-expired",
            title="Social Token Expired",
            status_code=401,
            detail=detail,
            code="ERR_SOCIAL_TOKEN_EXPIRED",
        )


class SocialPublishRequest(BaseModel):
    """One post ready to publish, with the token already decrypted by the caller."""

    platform: SocialPlatform
    external_account_id: str | None = None
    handle: str
    access_token: str
    title: str | None = None
    caption: str
    hashtags: list[str] = Field(default_factory=list)
    media_urls: list[str] = Field(default_factory=list)
    account_config: dict[str, Any] = Field(default_factory=dict)


class SocialPublishReceipt(BaseModel):
    """What the platform confirmed, kept verbatim enough to audit the publish."""

    platform: SocialPlatform
    external_post_id: str
    permalink: str | None = None
    remote_status: str = "published"
    raw_response: dict[str, Any] = Field(default_factory=dict)


class SocialPublisherPort(Protocol):
    """Injection point used by `SocialService`; the HTTP dispatcher is one implementation."""

    async def publish(self, req: SocialPublishRequest) -> SocialPublishReceipt: ...


# --------------------------------------------------------------------------- errors


def _error_detail(req: SocialPublishRequest, text: str) -> str:
    """Bound and scrub an upstream body before it can reach a problem document."""
    return _scrub_secret(text.strip()[:500], req.access_token)


def _media_required(platform: SocialPlatform) -> AppError:
    return AppError(
        slug="social-media-required",
        title="Media Required",
        status_code=422,
        detail=(
            f"{playbook_for(platform).label} publishing needs at least one publicly "
            "reachable media URL."
        ),
        code="ERR_SOCIAL_MEDIA_REQUIRED",
    )


def _config_required(platform: SocialPlatform, field: str) -> AppError:
    return AppError(
        slug="social-config-required",
        title="Social Account Config Required",
        status_code=422,
        detail=(
            f"{playbook_for(platform).label} publishing needs `{field}` on the connected "
            "account; connect the account again with that identifier."
        ),
        code="ERR_SOCIAL_CONFIG_REQUIRED",
    )


def _rate_limited(req: SocialPublishRequest, text: str) -> AppError:
    return AppError(
        slug="social-rate-limited",
        title="Social Platform Rate Limited",
        status_code=429,
        detail=_error_detail(
            req,
            f"{playbook_for(req.platform).label} rate limited this publish; retry later. "
            f"Upstream: {text}",
        ),
        code="ERR_SOCIAL_RATE_LIMITED",
    )


def _publish_failed(req: SocialPublishRequest, text: str) -> AppError:
    return AppError(
        slug="social-publish-failed",
        title="Social Publish Failed",
        status_code=502,
        detail=_error_detail(req, text),
        code="ERR_SOCIAL_PUBLISH_FAILED",
    )


def _manual_publish_only(platform: SocialPlatform) -> AppError:
    label = playbook_for(platform).label
    return AppError(
        slug="social-manual-publish-only",
        title="Manual Publishing Required",
        status_code=422,
        detail=(
            f"{label} exposes no third-party publishing API. Export the finished assets and "
            "publish them in the official creation console, then confirm the publish."
        ),
        code="ERR_SOCIAL_MANUAL_PUBLISH_ONLY",
    )


def _token_expired(req: SocialPublishRequest, text: str) -> SocialTokenExpiredError:
    return SocialTokenExpiredError(
        _error_detail(
            req,
            f"{playbook_for(req.platform).label} rejected the stored authorization: {text}",
        )
    )


# --------------------------------------------------------------------------- helpers


def _scrub_secret(text: str, secret: str) -> str:
    """Remove `secret` (raw and URL-encoded) from `text`.

    Every error detail and receipt string passes through here so an access token can
    never leak into a problem document, a structured log line, or the `social_posts`
    metrics JSON. An empty secret is a no-op (platforms without a bearer token).
    """
    if not secret:
        return text
    scrubbed = text.replace(secret, REDACTED)
    encoded = quote(secret, safe="")
    if encoded != secret:
        scrubbed = scrubbed.replace(encoded, REDACTED)
    return scrubbed


def _scrub_payload(value: Any, secret: str) -> Any:
    """Recursively scrub upstream payload strings kept in a receipt."""
    if not secret:
        return value
    if isinstance(value, str):
        return _scrub_secret(value, secret)
    if isinstance(value, Mapping):
        return {str(key): _scrub_payload(item, secret) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_payload(item, secret) for item in value]
    return value


def _normalized_hashtags(hashtags: Sequence[str]) -> list[str]:
    """Trim, de-`#`, drop empties and deduplicate case-insensitively, order preserved."""
    seen: set[str] = set()
    normalized: list[str] = []
    for raw in hashtags:
        tag = _HASHTAG_WHITESPACE.sub("", (raw or "").strip().lstrip("#"))
        if not tag:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(f"#{tag}")
    return normalized


def _truncate(text: str, max_length: int) -> str:
    """Cut `text` to at most `max_length` characters, preferring a word boundary."""
    if max_length <= 0:
        return ""
    if len(text) <= max_length:
        return text
    budget = max_length - len(ELLIPSIS)
    head = text[:budget]
    boundary = max(head.rfind(" "), head.rfind("\n"))
    if boundary > budget // 2:
        head = head[:boundary]
    head = head.rstrip(" \n\t,;:-—")
    return f"{head}{ELLIPSIS}"


def compose_social_text(
    caption: str,
    hashtags: Sequence[str],
    *,
    max_length: int | None = None,
) -> str:
    """Join `caption` with deduplicated `#tags`, optionally truncating safely.

    Passing `max_length` (280 for X, 2200 for a TikTok title) guarantees the returned
    string fits, ending in `…` instead of a half-written hashtag or word.
    """
    body = (caption or "").strip()
    tags = " ".join(_normalized_hashtags(hashtags))
    text = f"{body} {tags}".strip() if tags else body
    if max_length is None:
        return text
    return _truncate(text, max_length)


def _config_text(req: SocialPublishRequest, key: str) -> str | None:
    """Read a non-empty scalar from `account_config` without trusting its JSON type."""
    value = req.account_config.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int | float):
        return str(value)
    return None


def _first_media(req: SocialPublishRequest) -> str:
    for candidate in req.media_urls:
        media_url = (candidate or "").strip()
        if media_url:
            return media_url
    raise _media_required(req.platform)


def _is_video_url(media_url: str) -> bool:
    return urlsplit(media_url).path.lower().endswith(VIDEO_SUFFIXES)


def _required_id(
    req: SocialPublishRequest,
    payload: Mapping[str, Any],
    key: str,
    *,
    label: str,
) -> str:
    value = payload.get(key)
    if isinstance(value, bool) or value is None:
        raise _publish_failed(
            req,
            f"{playbook_for(req.platform).label} API returned no {label}.",
        )
    text = str(value).strip()
    if not text:
        raise _publish_failed(
            req,
            f"{playbook_for(req.platform).label} API returned no {label}.",
        )
    return text


def _first_id(
    req: SocialPublishRequest,
    payload: Mapping[str, Any],
    keys: Sequence[str],
    *,
    label: str,
) -> str:
    """Meta answers with `id`, `post_id`, or both depending on the edge; take the first."""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool) or value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    raise _publish_failed(
        req,
        f"{playbook_for(req.platform).label} API returned no {label}.",
    )


def _json_object(req: SocialPublishRequest, response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise _publish_failed(
            req,
            f"{playbook_for(req.platform).label} API returned a non-JSON body: {response.text}",
        ) from exc
    if not isinstance(payload, dict):
        raise _publish_failed(
            req,
            f"{playbook_for(req.platform).label} API returned an unexpected JSON shape.",
        )
    return {str(key): value for key, value in payload.items()}


def _ensure_success(req: SocialPublishRequest, response: httpx.Response) -> None:
    """Map HTTP status to the module's typed errors; 2xx/3xx bodies are the caller's job."""
    status = response.status_code
    if status < 400:
        return
    upstream = f"{playbook_for(req.platform).label} API responded {status}: {response.text}"
    if status in (401, 403):
        raise _token_expired(req, upstream)
    if status == 429:
        raise _rate_limited(req, upstream)
    raise _publish_failed(req, upstream)


def _wechat_error(req: SocialPublishRequest, payload: Mapping[str, Any]) -> None:
    """WeChat answers HTTP 200 with an `errcode`; 40001/42001 means the token is dead."""
    raw = payload.get("errcode", 0)
    if raw is None:
        return
    try:
        errcode = int(raw)
    except (TypeError, ValueError) as exc:
        raise _publish_failed(req, f"WeChat returned a non-numeric errcode: {raw!r}") from exc
    if errcode == 0:
        return
    message = str(payload.get("errmsg", ""))
    if errcode in WECHAT_TOKEN_ERRCODES:
        raise _token_expired(req, f"WeChat errcode {errcode}: {message}")
    raise _publish_failed(req, f"WeChat errcode {errcode}: {message}")


def _tiktok_error(req: SocialPublishRequest, payload: Mapping[str, Any]) -> None:
    """TikTok's Content Posting API reports failures inside a 200 envelope."""
    error = payload.get("error")
    if not isinstance(error, Mapping):
        return
    code = str(error.get("code", "") or "").strip()
    if not code or code == "ok":
        return
    message = str(error.get("message", ""))
    if code in _TIKTOK_TOKEN_ERROR_CODES:
        raise _token_expired(req, f"TikTok error {code}: {message}")
    raise _publish_failed(req, f"TikTok error {code}: {message}")


def _receipt(
    req: SocialPublishRequest,
    *,
    external_post_id: str,
    payload: Mapping[str, Any],
    permalink: str | None = None,
    remote_status: str = "published",
) -> SocialPublishReceipt:
    scrubbed = _scrub_payload(dict(payload), req.access_token)
    raw: dict[str, Any] = scrubbed if isinstance(scrubbed, dict) else {}
    return SocialPublishReceipt(
        platform=req.platform,
        external_post_id=_scrub_secret(external_post_id, req.access_token),
        permalink=_scrub_secret(permalink, req.access_token) if permalink else None,
        remote_status=remote_status,
        raw_response=raw,
    )


# --------------------------------------------------------------------------- dispatcher


class HttpSocialPublisherDispatcher:
    """Publishes through the eight official APIs; every other platform is refused.

    `transport` is injected so the unit suite runs with `httpx.MockTransport` and the
    composition root decides on real networking. Nothing here retries: a 429/5xx
    propagates to the scheduler, which owns backoff.
    """

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._transport = transport
        self._timeout = timeout

    async def publish(self, req: SocialPublishRequest) -> SocialPublishReceipt:
        if req.platform not in API_PLATFORMS:
            raise _manual_publish_only(req.platform)
        async with httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
            headers={"Accept": "application/json", "User-Agent": "ai-native-erp/social"},
        ) as client:
            try:
                return await self._dispatch(client, req)
            except AppError:
                # Already typed, already scrubbed (every detail went through `_scrub_secret`).
                raise
            except httpx.HTTPError as exc:
                # Transport errors embed the full URL — which carries WeChat's token.
                raise _publish_failed(
                    req,
                    f"Network error talking to {playbook_for(req.platform).label}: "
                    f"{type(exc).__name__}: {exc}",
                ) from exc

    async def _dispatch(
        self, client: httpx.AsyncClient, req: SocialPublishRequest
    ) -> SocialPublishReceipt:
        if req.platform == "instagram":
            return await self._publish_instagram(client, req)
        if req.platform == "facebook":
            return await self._publish_facebook(client, req)
        if req.platform == "tiktok":
            return await self._publish_tiktok(client, req)
        if req.platform == "youtube":
            return await self._publish_youtube(client, req)
        if req.platform == "linkedin":
            return await self._publish_linkedin(client, req)
        if req.platform == "pinterest":
            return await self._publish_pinterest(client, req)
        if req.platform == "x":
            return await self._publish_x(client, req)
        if req.platform == "wechat_oa":
            return await self._publish_wechat_oa(client, req)
        # Fail closed: an API platform added to the set above needs a real adapter here.
        raise _manual_publish_only(req.platform)

    # -- Meta: Instagram Graph API v21.0 ----------------------------------

    async def _publish_instagram(
        self, client: httpx.AsyncClient, req: SocialPublishRequest
    ) -> SocialPublishReceipt:
        ig_user_id = (req.external_account_id or "").strip() or _config_text(req, "ig_user_id")
        if not ig_user_id:
            raise _config_required("instagram", "ig_user_id")
        media_url = _first_media(req)
        caption = compose_social_text(req.caption, req.hashtags)
        headers = {"Authorization": f"Bearer {req.access_token}"}
        container: dict[str, Any] = {"caption": caption}
        if _is_video_url(media_url):
            container["media_type"] = "REELS"
            container["video_url"] = media_url
        else:
            container["image_url"] = media_url

        created = await client.post(
            f"{GRAPH_API_BASE}/{ig_user_id}/media", json=container, headers=headers
        )
        _ensure_success(req, created)
        created_payload = _json_object(req, created)
        creation_id = _required_id(req, created_payload, "id", label="media container id")

        published = await client.post(
            f"{GRAPH_API_BASE}/{ig_user_id}/media_publish",
            json={"creation_id": creation_id},
            headers=headers,
        )
        _ensure_success(req, published)
        published_payload = _json_object(req, published)
        # The Graph `id` is a numeric media id, not the shortcode a permalink needs.
        return _receipt(
            req,
            external_post_id=_required_id(req, published_payload, "id", label="published media id"),
            payload=published_payload,
        )

    # -- Meta: Facebook Pages API v21.0 -----------------------------------

    async def _publish_facebook(
        self, client: httpx.AsyncClient, req: SocialPublishRequest
    ) -> SocialPublishReceipt:
        page_id = (req.external_account_id or "").strip() or _config_text(req, "page_id")
        if not page_id:
            raise _config_required("facebook", "page_id")
        message = compose_social_text(req.caption, req.hashtags)
        headers = {"Authorization": f"Bearer {req.access_token}"}
        has_media = any((url or "").strip() for url in req.media_urls)
        if has_media:
            media_url = _first_media(req)
            response = await client.post(
                f"{GRAPH_API_BASE}/{page_id}/photos",
                data={"url": media_url, "caption": message},
                headers=headers,
            )
        else:
            response = await client.post(
                f"{GRAPH_API_BASE}/{page_id}/feed",
                data={"message": message},
                headers=headers,
            )
        _ensure_success(req, response)
        payload = _json_object(req, response)
        post_id = _first_id(req, payload, ("post_id", "id"), label="post id")
        return _receipt(
            req,
            external_post_id=post_id,
            payload=payload,
            permalink=f"https://www.facebook.com/{post_id}",
        )

    # -- TikTok Content Posting API v2 ------------------------------------

    async def _publish_tiktok(
        self, client: httpx.AsyncClient, req: SocialPublishRequest
    ) -> SocialPublishReceipt:
        media_url = _first_media(req)
        title = (req.title or compose_social_text(req.caption, req.hashtags)).strip()
        title = _truncate(title, TIKTOK_MAX_TITLE_LENGTH)
        # Compliance: until the app passes TikTok's audit, posts stay SELF_ONLY.
        privacy_level = _config_text(req, "privacy_level") or "SELF_ONLY"
        body = {
            "post_info": {"title": title, "privacy_level": privacy_level},
            "source_info": {"source": "PULL_FROM_URL", "video_url": media_url},
        }
        response = await client.post(
            f"{TIKTOK_API_BASE}/post/publish/video/init/",
            json=body,
            headers={"Authorization": f"Bearer {req.access_token}"},
        )
        _ensure_success(req, response)
        payload = _json_object(req, response)
        _tiktok_error(req, payload)
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise _publish_failed(req, "TikTok API returned no publish data.")
        publish_id = _required_id(req, data, "publish_id", label="publish_id")
        return _receipt(
            req,
            external_post_id=publish_id,
            payload=payload,
            remote_status="uploading",
        )

    # -- YouTube Data API v3 ----------------------------------------------

    async def _publish_youtube(
        self, client: httpx.AsyncClient, req: SocialPublishRequest
    ) -> SocialPublishReceipt:
        title = (req.title or req.caption).strip()
        if not title:
            raise _publish_failed(req, "YouTube uploads require a non-empty title.")
        description = (req.caption or "").strip()
        privacy_status = _config_text(req, "privacy_status") or "private"
        body = {
            "snippet": {
                "title": title[:YOUTUBE_MAX_TITLE_LENGTH],
                "description": description,
                "tags": [tag.lstrip("#") for tag in _normalized_hashtags(req.hashtags)],
            },
            "status": {"privacyStatus": privacy_status},
        }
        response = await client.post(
            f"{YOUTUBE_API_BASE}/videos",
            params={"part": "snippet,status"},
            json=body,
            headers={"Authorization": f"Bearer {req.access_token}"},
        )
        _ensure_success(req, response)
        payload = _json_object(req, response)
        video_id = _required_id(req, payload, "id", label="video id")
        return _receipt(
            req,
            external_post_id=video_id,
            payload=payload,
            permalink=f"https://www.youtube.com/watch?v={video_id}",
        )

    # -- LinkedIn REST Posts API ------------------------------------------

    async def _publish_linkedin(
        self, client: httpx.AsyncClient, req: SocialPublishRequest
    ) -> SocialPublishReceipt:
        raw_author = (req.external_account_id or "").strip() or _config_text(req, "author_urn")
        if not raw_author:
            raise _config_required("linkedin", "author_urn")
        if raw_author.startswith("urn:li:"):
            author_urn = raw_author
        else:
            author_urn = f"urn:li:organization:{raw_author}"
        body = {
            "author": author_urn,
            "commentary": compose_social_text(req.caption, req.hashtags),
            "visibility": "PUBLIC",
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
        }
        response = await client.post(
            f"{LINKEDIN_API_BASE}/rest/posts",
            json=body,
            headers={
                "Authorization": f"Bearer {req.access_token}",
                "X-Restli-Protocol-Version": "2.0.0",
                "LinkedIn-Version": "202401",
            },
        )
        _ensure_success(req, response)
        payload: dict[str, Any] = {}
        if response.content:
            payload = _json_object(req, response)
        restli_id = (response.headers.get("x-restli-id") or "").strip()
        external_post_id = restli_id or _required_id(req, payload, "id", label="post id")
        permalink = (
            f"https://www.linkedin.com/feed/update/{external_post_id}/"
            if external_post_id.startswith("urn:li:")
            else None
        )
        return _receipt(
            req,
            external_post_id=external_post_id,
            payload=payload,
            permalink=permalink,
        )

    # -- Pinterest API v5 -------------------------------------------------

    async def _publish_pinterest(
        self, client: httpx.AsyncClient, req: SocialPublishRequest
    ) -> SocialPublishReceipt:
        board_id = _config_text(req, "board_id") or (req.external_account_id or "").strip()
        if not board_id:
            raise _config_required("pinterest", "board_id")
        media_url = _first_media(req)
        title = (req.title or req.caption).strip()[:100]
        body = {
            "board_id": board_id,
            "title": title,
            "description": compose_social_text(req.caption, req.hashtags),
            "media_source": {"source_type": "image_url", "url": media_url},
        }
        response = await client.post(
            f"{PINTEREST_API_BASE}/v5/pins",
            json=body,
            headers={"Authorization": f"Bearer {req.access_token}"},
        )
        _ensure_success(req, response)
        payload = _json_object(req, response)
        pin_id = _required_id(req, payload, "id", label="pin id")
        return _receipt(
            req,
            external_post_id=pin_id,
            payload=payload,
            permalink=f"https://www.pinterest.com/pin/{pin_id}/",
        )

    # -- X API v2 ---------------------------------------------------------

    async def _publish_x(
        self, client: httpx.AsyncClient, req: SocialPublishRequest
    ) -> SocialPublishReceipt:
        text = compose_social_text(req.caption, req.hashtags, max_length=X_MAX_TEXT_LENGTH)
        if not text:
            raise _publish_failed(req, "X posts require non-empty text.")
        response = await client.post(
            f"{X_API_BASE}/2/tweets",
            json={"text": text},
            headers={"Authorization": f"Bearer {req.access_token}"},
        )
        _ensure_success(req, response)
        payload = _json_object(req, response)
        data = payload.get("data")
        if not isinstance(data, Mapping):
            raise _publish_failed(req, "X API returned no tweet data.")
        tweet_id = _required_id(req, data, "id", label="tweet id")
        return _receipt(
            req,
            external_post_id=tweet_id,
            payload=payload,
            permalink=f"https://x.com/i/web/status/{tweet_id}",
        )

    # -- WeChat Official Account: draft/add + freepublish/submit ----------

    async def _publish_wechat_oa(
        self, client: httpx.AsyncClient, req: SocialPublishRequest
    ) -> SocialPublishReceipt:
        title = (req.title or req.caption).strip()[:WECHAT_MAX_TITLE_LENGTH]
        if not title:
            raise _publish_failed(req, "WeChat Official Account drafts require a title.")
        article = {
            "title": title,
            "content": compose_social_text(req.caption, req.hashtags),
            "thumb_media_id": _config_text(req, "thumb_media_id") or "",
        }
        token_params = {"access_token": req.access_token}

        draft_response = await client.post(
            f"{WECHAT_OA_API_BASE}/draft/add",
            params=token_params,
            json={"articles": [article]},
        )
        _ensure_success(req, draft_response)
        draft_payload = _json_object(req, draft_response)
        _wechat_error(req, draft_payload)
        media_id = _required_id(req, draft_payload, "media_id", label="draft media_id")

        publish_response = await client.post(
            f"{WECHAT_OA_API_BASE}/freepublish/submit",
            params=token_params,
            json={"media_id": media_id},
        )
        _ensure_success(req, publish_response)
        publish_payload = _json_object(req, publish_response)
        _wechat_error(req, publish_payload)
        publish_id = _required_id(req, publish_payload, "publish_id", label="publish_id")
        return _receipt(
            req,
            external_post_id=publish_id,
            payload=publish_payload,
            remote_status="submitted",
        )
