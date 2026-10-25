import sqlite3

from app.analytics.service import (
    NOT_AVAILABLE,
    counts,
    engagement_view,
    parse_facebook_engagement,
    parse_instagram_engagement,
    record_official_metric,
    refresh_engagement,
    suggest_topics,
)
from app.transport import HttpResult
from tests.helpers import FakeTransport, make_settings


def test_counts_and_suggestions_come_from_rows(db):
    stats = counts(db)
    assert stats == {
        "generated": 0,
        "approved": 0,
        "published": 0,
        "failed": 0,
        "inquiries_by_source": [],
    }
    assert engagement_view(db)["status"] == NOT_AVAILABLE
    assert any("No drafts yet" in line for line in suggest_topics(db))
    assert parse_facebook_engagement({"id": "1"}) == []
    assert parse_instagram_engagement({"id": "1"}) == []
    parsed = parse_facebook_engagement(
        {"id": "1", "reactions": {"summary": {"total_count": 4}}, "comments": {"summary": {"total_count": 1}}}
    )
    assert ("reactions", 4.0) in parsed
    record_official_metric(
        db,
        platform="facebook",
        external_id="1",
        metric_name="reactions",
        metric_value=4,
        raw_payload='{"id":"1"}',
    )
    db.commit()
    view = engagement_view(db)
    assert view["status"] == "official_api"
    assert view["metrics"][0]["metric_value"] == 4
    assert pytest_raises_integrity(db) is True


def pytest_raises_integrity(db):
    import pytest

    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            """
            INSERT INTO engagement_snapshots (
                platform, external_id, metric_name, metric_value, source, fetched_at, raw_payload
            ) VALUES ('facebook', '1', 'likes', 9, 'guessed', 't', '{}')
            """
        )
    return True


def test_refresh_without_credentials_invents_nothing(db, tmp_path):
    settings = make_settings(tmp_path)
    transport = FakeTransport(lambda *args: (_ for _ in ()).throw(AssertionError("called")))
    result = refresh_engagement(db, settings, transport)
    assert result["status"] == NOT_AVAILABLE
    assert "invented" in result["detail"]
    assert transport.calls == []
    assert engagement_view(db)["status"] == NOT_AVAILABLE


def test_refresh_stores_only_fields_the_payload_contains(db, tmp_path):
    settings = make_settings(tmp_path, facebook_page_id="10", facebook_page_token="token")
    db.execute(
        """
        INSERT INTO drafts (
            topic_key, category, platform, hook, english_examples_json, korean_explanation,
            engagement_question, hashtags_json, content_status, created_at, updated_at
        ) VALUES ('t', 'interview_prep', 'facebook', 'h', '[]', 'k', 'q', '[]', 'approved', 't', 't')
        """
    )
    draft_id = db.execute("SELECT id FROM drafts").fetchone()["id"]
    db.execute(
        """
        INSERT INTO publish_items (
            draft_id, platform, status, idempotency_key, external_id, confirmed_success,
            prepared_text, hashtags_json, created_at, updated_at
        ) VALUES (?, 'facebook', 'published', 'idem-1', '10_20', 1, 'text', '[]', 't', 't')
        """,
        (draft_id,),
    )
    db.commit()

    def handler(method, url, kwargs):
        assert "access_token" in kwargs["params"]
        if kwargs.get("marker") == "empty":
            return HttpResult(200, '{"id":"10_20"}')
        return HttpResult(200, '{"id":"10_20","reactions":{"summary":{"total_count":2}}}')

    result = refresh_engagement(db, settings, FakeTransport(handler))
    db.commit()
    assert result["stored"] == 1
    assert engagement_view(db)["metrics"][0]["metric_name"] == "reactions"
    assert parse_instagram_engagement({"like_count": 3, "comments_count": 0})[1] == ("comments_count", 0.0)
