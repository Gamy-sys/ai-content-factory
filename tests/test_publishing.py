import json
import sqlite3

import pytest

from app.publishing.adapters import NaverBlogAdapter, encode_cafe_field, get_adapter
from app.publishing.service import (
    PublishRefused,
    acknowledge_uncertain,
    assert_can_transition,
    attempt_publish,
    export_text,
    queue_draft,
)
from app.transport import HttpResult, TransportError
from tests.helpers import FakeTransport, valid_draft
from app.content.service import approve_draft, save_edit
from app.db import connect, init_db
from tests.helpers import make_settings


def _draft(conn, **overrides):
    from app.content.service import save_new_draft
    from app.content.schema import DraftContent

    model = DraftContent.model_validate(valid_draft(**overrides))
    draft = save_new_draft(conn, model, model.model_dump(), 1)
    return approve_draft(conn, draft["id"], True)


def test_encode_cafe_field_uses_ms949():
    assert encode_cafe_field("가") == "%B0%A1"


def test_state_machine_refuses_published_shortcut():
    with pytest.raises(PublishRefused, match="Refusing to mark published"):
        assert_can_transition("approved", "published")
    with pytest.raises(PublishRefused, match="Refusing to mark published"):
        assert_can_transition("draft", "published")
    assert_can_transition("approved", "publishing")


def test_published_without_confirmation_is_rejected_by_sqlite(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO publish_items (
                draft_id, platform, status, idempotency_key, prepared_text, hashtags_json,
                confirmed_success, created_at, updated_at
            ) VALUES (1, 'facebook', 'published', 'key', 'text', '[]', 0, 't', 't')
            """
        )


def test_naver_blog_is_manual_and_export_does_not_publish(db):
    draft = _draft(db, platform="naver_blog", topic_key="blog-manual")
    item = queue_draft(db, draft["id"])
    db.commit()
    assert item["status"] == "manual-action-required"
    assert NaverBlogAdapter.automated is False
    text = export_text(db, item["id"])
    assert "#영어공부" in text
    assert "Been and gone" in text
    transport = FakeTransport(lambda *args: (_ for _ in ()).throw(AssertionError("network")))
    result = attempt_publish(db, item["id"], make_settings_from(db), transport)
    assert result["published"] is False
    assert "Nothing was published" in result["detail"]
    assert transport.calls == []
    assert item["status"] == "manual-action-required"


def make_settings_from(conn):
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    from pathlib import Path

    folder = Path(path).parent
    return make_settings(folder, database_path=path)


def test_facebook_success_is_idempotent_and_redacts_token(db):
    draft = _draft(db, platform="facebook", topic_key="page-post")
    item = queue_draft(db, draft["id"])
    db.commit()
    settings = make_settings_from(db)
    settings.facebook_page_id = "123456"
    settings.facebook_page_token = "SUPERSECRETTOKEN"

    def handler(method, url, kwargs):
        assert kwargs["data"]["access_token"] == "SUPERSECRETTOKEN"
        return HttpResult(200, '{"id":"123_999","echo":"SUPERSECRETTOKEN"}')

    transport = FakeTransport(handler)
    first = attempt_publish(db, item["id"], settings, transport)
    assert first["published"] is True
    assert first["item"]["external_id"] == "123_999"
    stored = db.execute("SELECT detail FROM publish_attempts").fetchone()["detail"]
    assert "SUPERSECRETTOKEN" not in stored
    assert "SUPERSECRETTOKEN" not in (first["item"]["last_error"] or "")
    second = attempt_publish(db, item["id"], settings, transport)
    assert second["detail"].startswith("Already published")
    assert len(transport.calls) == 1


def test_facebook_without_credentials_does_not_call_or_publish(db):
    draft = _draft(db, platform="facebook", topic_key="no-token")
    item = queue_draft(db, draft["id"])
    db.commit()
    transport = FakeTransport(lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    result = attempt_publish(db, item["id"], make_settings_from(db), transport)
    assert result["published"] is False
    assert "Nothing was published" in result["detail"]
    assert result["item"]["status"] == "approved"
    assert transport.calls == []


def test_uncertain_timeout_blocks_retry_until_acknowledged(db):
    draft = _draft(db, platform="facebook", topic_key="timeout-post")
    item = queue_draft(db, draft["id"])
    db.commit()
    settings = make_settings_from(db)
    settings.facebook_page_id = "55"
    settings.facebook_page_token = "token"

    def boom(method, url, kwargs):
        raise TransportError("timed out", uncertain=True)

    transport = FakeTransport(boom)
    # Adapter catches TransportError, so raise from the transport and let the adapter convert it.
    first = attempt_publish(db, item["id"], settings, transport)
    assert first["published"] is False
    assert first["item"]["status"] == "failed"
    assert first["item"]["retry_blocked"] is True
    second = attempt_publish(db, item["id"], settings, transport)
    assert "Nothing was sent" in second["detail"]
    assert len(transport.calls) == 1
    acknowledge_uncertain(db, item["id"])
    db.commit()
    third = attempt_publish(db, item["id"], settings, transport)
    assert third["published"] is False
    assert len(transport.calls) == 2


def test_clear_http_failure_can_be_retried(db):
    draft = _draft(db, platform="facebook", topic_key="rejected-post")
    item = queue_draft(db, draft["id"])
    db.commit()
    settings = make_settings_from(db)
    settings.facebook_page_id = "55"
    settings.facebook_page_token = "token"
    calls = {"n": 0}

    def handler(method, url, kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return HttpResult(400, '{"error":{"message":"bad request"}}')
        return HttpResult(200, '{"id":"55_1"}')

    result = attempt_publish(db, item["id"], settings, FakeTransport(handler))
    assert result["published"] is False
    assert result["item"]["retry_blocked"] is False
    second = attempt_publish(db, item["id"], settings, FakeTransport(handler))
    assert second["published"] is True


def test_instagram_text_only_does_not_call(db):
    draft = _draft(db, platform="instagram", topic_key="caption-only")
    item = queue_draft(db, draft["id"])
    db.commit()
    settings = make_settings_from(db)
    settings.instagram_user_id = "99"
    settings.instagram_access_token = "token"
    transport = FakeTransport(lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
    result = attempt_publish(db, item["id"], settings, transport)
    assert result["published"] is False
    assert "public https" in result["detail"]
    assert transport.calls == []
    assert result["item"]["status"] == "approved"


def test_instagram_two_step_publish(db):
    draft = _draft(db, platform="instagram", topic_key="image-post")
    item = queue_draft(db, draft["id"])
    db.commit()
    settings = make_settings_from(db)
    settings.instagram_user_id = "99"
    settings.instagram_access_token = "token"
    settings.instagram_image_url = "https://example.com/lesson.jpg"

    def handler(method, url, kwargs):
        if url.endswith("/media"):
            return HttpResult(200, '{"id":"container-1"}')
        if url.endswith("/media_publish"):
            return HttpResult(200, '{"id":"media-9"}')
        raise AssertionError(url)

    result = attempt_publish(db, item["id"], settings, FakeTransport(handler))
    assert result["published"] is True
    assert result["item"]["external_id"] == "media-9"


def test_naver_cafe_success_and_body_status_and_cap(db):
    from urllib.parse import unquote_to_bytes

    draft = _draft(db, platform="naver_cafe", topic_key="cafe-post")
    item = queue_draft(db, draft["id"])
    db.commit()
    settings = make_settings_from(db)
    settings.naver_cafe_access_token = "cafe-token"
    settings.naver_cafe_club_id = "10"
    settings.naver_cafe_menu_id = "3"
    settings.naver_cafe_daily_cap = 1
    seen = {}

    def handler(method, url, kwargs):
        seen["body"] = kwargs["content"].decode()
        return HttpResult(
            200,
            json.dumps(
                {
                    "message": {
                        "status": "200",
                        "result": {"articleId": 4, "articleUrl": "https://cafe.naver.com/example/4"},
                    }
                }
            ),
        )

    result = attempt_publish(db, item["id"], settings, FakeTransport(handler))
    assert result["published"] is True
    assert result["item"]["external_url"].endswith("/4")
    decoded = unquote_to_bytes(seen["body"].split("&content=", 1)[1]).decode("ms949")
    assert "한국" in decoded
    other = _draft(db, platform="naver_cafe", topic_key="cafe-second")
    second_item = queue_draft(db, other["id"])
    db.commit()
    transport = FakeTransport(handler)
    blocked = attempt_publish(db, second_item["id"], settings, transport)
    assert blocked["published"] is False
    assert "Nothing was published" in blocked["detail"]
    assert transport.calls == []

    failed = _draft(db, platform="naver_cafe", topic_key="cafe-failed")
    # Cap is full, so use a fresh settings cap after simulating a non-success response on a new database path.
    assert get_adapter("naver_blog").automated is False


def test_cafe_body_failure_is_not_published(db):
    draft = _draft(db, platform="naver_cafe", topic_key="cafe-reject")
    item = queue_draft(db, draft["id"])
    db.commit()
    settings = make_settings_from(db)
    settings.naver_cafe_access_token = "cafe-token"
    settings.naver_cafe_club_id = "10"
    settings.naver_cafe_menu_id = "3"

    def handler(method, url, kwargs):
        return HttpResult(200, '{"message":{"status":"500","error":"nope"}}')

    result = attempt_publish(db, item["id"], settings, FakeTransport(handler))
    assert result["published"] is False
    assert result["item"]["status"] == "failed"
    assert result["item"]["confirmed_success"] is False


def test_queue_requires_approval_and_is_unique(db):
    from app.content.schema import DraftContent
    from app.content.service import save_new_draft

    model = DraftContent.model_validate(valid_draft(platform="facebook", topic_key="needs-approval"))
    draft = save_new_draft(db, model, model.model_dump(), 1)
    with pytest.raises(PublishRefused, match="Nothing was queued"):
        queue_draft(db, draft["id"])
    save_edit(db, draft["id"], valid_draft(platform="facebook", topic_key="needs-approval"))
    approved = approve_draft(db, draft["id"], False)
    first = queue_draft(db, approved["id"])
    second = queue_draft(db, approved["id"])
    assert first["id"] == second["id"]
