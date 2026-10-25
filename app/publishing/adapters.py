import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from app.config import Settings
from app.transport import HttpResult, TransportError

GRAPH_VERSION = re.compile(r"^v\d+\.\d+$")


@dataclass
class PublishOutcome:
    state: str
    confirmed: bool = False
    external_id: str | None = None
    external_url: str | None = None
    detail: str = ""
    uncertain: bool = False
    http_attempted: bool = False
    response_text: str = ""


def encode_cafe_field(value: str) -> str:
    """Encode text the way the current Naver Cafe write docs require.

    The official guide says to take the UTF-8 text, re-encode it as MS949,
    then percent-encode those bytes.
    """
    try:
        raw = value.encode("ms949")
    except UnicodeEncodeError as exc:
        raise CafeEncodingError(
            "Cafe text could not be encoded as MS949 for the official Naver API. Nothing was published."
        ) from exc
    return "".join(f"%{byte:02X}" for byte in raw)


class CafeEncodingError(ValueError):
    pass


def _graph_version(settings: Settings) -> str:
    if GRAPH_VERSION.fullmatch(settings.meta_graph_version or ""):
        return settings.meta_graph_version
    return "v26.0"


def _public_image(settings: Settings) -> str | None:
    url = (settings.instagram_image_url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return url


def _error_uncertain(status_code: int) -> bool:
    return status_code >= 500 or status_code == 429


def _graph_success(result: HttpResult) -> tuple[str | None, bool, str]:
    if result.status_code != 200:
        return None, _error_uncertain(result.status_code), f"HTTP {result.status_code}"
    try:
        data = result.json()
    except (json.JSONDecodeError, ValueError):
        return None, True, "The platform returned a response that was not JSON."
    if data.get("error"):
        message = data["error"].get("message") if isinstance(data["error"], dict) else data["error"]
        return None, False, f"The platform rejected the post: {message}"
    ident = data.get("id")
    if not ident:
        return None, True, "The platform returned HTTP 200 without a post id."
    return str(ident), False, ""


class FacebookAdapter:
    platform = "facebook"
    automated = True
    summary = (
        "Facebook Pages can be posted with the official Graph API "
        "(POST /{page-id}/feed) when a Page access token and pages_manage_posts are configured."
    )

    def credentials_ready(self, settings: Settings) -> bool:
        return bool(settings.facebook_page_id and settings.facebook_page_token)

    def publish(self, text: str, settings: Settings, transport) -> PublishOutcome:
        if not settings.facebook_page_id.isdigit():
            return PublishOutcome(
                state="failed",
                detail="The Facebook Page id must be numeric. Nothing was published.",
            )
        url = f"https://graph.facebook.com/{_graph_version(settings)}/{settings.facebook_page_id}/feed"
        try:
            result = transport.request(
                "POST",
                url,
                data={"message": text, "access_token": settings.facebook_page_token},
                timeout=30,
            )
        except TransportError as exc:
            return PublishOutcome(
                state="failed",
                detail=str(exc),
                uncertain=exc.uncertain,
                http_attempted=True,
            )
        post_id, uncertain, detail = _graph_success(result)
        if not post_id:
            return PublishOutcome(
                state="failed",
                detail=detail or "Facebook did not confirm a post id. Nothing was marked published.",
                uncertain=uncertain,
                http_attempted=True,
                response_text=result.text[:2000],
            )
        return PublishOutcome(
            state="published",
            confirmed=True,
            external_id=post_id,
            detail=f"Facebook confirmed post {post_id}.",
            http_attempted=True,
            response_text=result.text[:2000],
        )


class InstagramAdapter:
    platform = "instagram"
    automated = True
    summary = (
        "Instagram's official Content Publishing API can publish a public image plus a caption "
        "for a professional account. It cannot publish a text-only post, and the image must already "
        "be on a public https URL because Meta downloads it. This local app does not host public files."
    )

    def credentials_ready(self, settings: Settings) -> bool:
        return bool(settings.instagram_user_id and settings.instagram_access_token)

    def image_ready(self, settings: Settings) -> bool:
        return _public_image(settings) is not None

    def publish(self, text: str, settings: Settings, transport) -> PublishOutcome:
        image = _public_image(settings)
        if image is None:
            return PublishOutcome(
                state="manual-action-required",
                detail=(
                    "Instagram's official API only publishes media on a public https URL. "
                    "No text-only post was sent."
                ),
            )
        if not settings.instagram_user_id.isdigit():
            return PublishOutcome(
                state="failed",
                detail="The Instagram user id must be numeric. Nothing was published.",
            )
        if len(text) > 2200:
            return PublishOutcome(
                state="failed",
                detail="The caption is longer than Instagram's 2200 character limit. Nothing was published.",
            )
        version = _graph_version(settings)
        user_id = settings.instagram_user_id
        create_url = f"https://graph.facebook.com/{version}/{user_id}/media"
        try:
            created = transport.request(
                "POST",
                create_url,
                data={
                    "image_url": image,
                    "caption": text,
                    "access_token": settings.instagram_access_token,
                },
                timeout=30,
            )
        except TransportError as exc:
            return PublishOutcome(
                state="failed",
                detail=str(exc),
                uncertain=exc.uncertain,
                http_attempted=True,
            )
        container_id, uncertain, detail = _graph_success(created)
        if not container_id:
            return PublishOutcome(
                state="failed",
                detail=detail or "Instagram did not create a media container. Nothing was published.",
                uncertain=uncertain,
                http_attempted=True,
                response_text=created.text[:2000],
            )
        publish_url = f"https://graph.facebook.com/{version}/{user_id}/media_publish"
        try:
            published = transport.request(
                "POST",
                publish_url,
                data={
                    "creation_id": container_id,
                    "access_token": settings.instagram_access_token,
                },
                timeout=30,
            )
        except TransportError as exc:
            return PublishOutcome(
                state="failed",
                detail=str(exc),
                uncertain=True,
                http_attempted=True,
                response_text=created.text[:1000],
            )
        media_id, uncertain, detail = _graph_success(published)
        combined = (created.text + "\n" + published.text)[:2000]
        if not media_id:
            return PublishOutcome(
                state="failed",
                detail=detail or "Instagram did not confirm the media id. Nothing was marked published.",
                uncertain=uncertain or True,
                http_attempted=True,
                response_text=combined,
            )
        return PublishOutcome(
            state="published",
            confirmed=True,
            external_id=media_id,
            detail=f"Instagram confirmed media {media_id}.",
            http_attempted=True,
            response_text=combined,
        )


class NaverBlogAdapter:
    platform = "naver_blog"
    automated = False
    summary = (
        "Naver shut down the Blog write Open API on 6 May 2020 "
        "(developers.naver.com notice 7527). There is no official automated publishing flow."
    )

    def credentials_ready(self, settings: Settings) -> bool:
        return False

    def publish(self, text: str, settings: Settings, transport) -> PublishOutcome:
        del text, settings, transport
        return PublishOutcome(state="manual-action-required", detail=self.summary)


class NaverCafeAdapter:
    platform = "naver_cafe"
    automated = True
    summary = (
        "Naver's official Cafe write API can post to a board you belong to "
        "(POST /v1/cafe/{clubid}/menu/{menuid}/articles) with a Naver Login access token. "
        "The limit is 200 posts a day per Naver account. Product boards are not supported. "
        "Cafe rules may still forbid promotion."
    )

    def credentials_ready(self, settings: Settings) -> bool:
        return bool(
            settings.naver_cafe_access_token
            and settings.naver_cafe_club_id
            and settings.naver_cafe_menu_id
        )

    def publish(self, text: str, settings: Settings, transport, *, subject: str) -> PublishOutcome:
        if not settings.naver_cafe_club_id.isdigit() or not settings.naver_cafe_menu_id.isdigit():
            return PublishOutcome(
                state="failed",
                detail="Cafe id and menu id must be numeric. Nothing was published.",
            )
        try:
            body = (
                f"subject={encode_cafe_field(subject[:200])}"
                f"&content={encode_cafe_field(text)}"
            )
        except CafeEncodingError as exc:
            return PublishOutcome(state="failed", detail=str(exc))
        url = (
            "https://openapi.naver.com/v1/cafe/"
            f"{settings.naver_cafe_club_id}/menu/{settings.naver_cafe_menu_id}/articles"
        )
        try:
            result = transport.request(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {settings.naver_cafe_access_token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content=body.encode("ascii"),
                timeout=30,
            )
        except TransportError as exc:
            return PublishOutcome(
                state="failed",
                detail=str(exc),
                uncertain=exc.uncertain,
                http_attempted=True,
            )
        article_id, article_url, uncertain, detail = _cafe_success(result)
        if not article_id:
            return PublishOutcome(
                state="failed",
                detail=detail,
                uncertain=uncertain,
                http_attempted=True,
                response_text=result.text[:2000],
            )
        return PublishOutcome(
            state="published",
            confirmed=True,
            external_id=article_id,
            external_url=article_url,
            detail=f"Naver Cafe confirmed article {article_id}.",
            http_attempted=True,
            response_text=result.text[:2000],
        )


def _cafe_success(result: HttpResult) -> tuple[str | None, str | None, bool, str]:
    if result.status_code != 200:
        return None, None, _error_uncertain(result.status_code), f"Naver returned HTTP {result.status_code}. Nothing was marked published."
    try:
        data = result.json()
    except (json.JSONDecodeError, ValueError):
        return None, None, True, "Naver returned a response that was not JSON. Nothing was marked published."
    message = data.get("message") if isinstance(data, dict) else None
    if not isinstance(message, dict):
        return None, None, True, "Naver's response had no message object. Nothing was marked published."
    if str(message.get("status")) != "200":
        return None, None, False, "Naver did not return status 200 in the body. Nothing was marked published."
    result_body = message.get("result") if isinstance(message.get("result"), dict) else {}
    article_id = result_body.get("articleId")
    if not article_id:
        return None, None, True, "Naver did not return an article id. Nothing was marked published."
    url = result_body.get("articleUrl")
    return str(article_id), str(url) if url else None, False, ""


ADAPTERS = {
    "facebook": FacebookAdapter(),
    "instagram": InstagramAdapter(),
    "naver_blog": NaverBlogAdapter(),
    "naver_cafe": NaverCafeAdapter(),
}


def get_adapter(platform: str):
    try:
        return ADAPTERS[platform]
    except KeyError as exc:
        raise KeyError(platform) from exc
