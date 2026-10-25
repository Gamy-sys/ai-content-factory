import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import bind_host
from app.main import create_app
from tests.helpers import make_settings, valid_draft
from tests.ollama_server import FakeOllama

ROOT = Path(__file__).resolve().parents[1]


def test_bind_host_is_loopback_only():
    assert bind_host("0.0.0.0") == "127.0.0.1"
    assert bind_host(None) == "127.0.0.1"
    script = (ROOT / "scripts" / "run-background.sh").read_text()
    makefile = (ROOT / "Makefile").read_text()
    assert "127.0.0.1" in script
    assert "0.0.0.0" not in script
    assert "127.0.0.1" in makefile
    assert "0.0.0.0" not in makefile


def test_pages_validation_and_ollama_failure(client):
    for path in [
        "/",
        "/calendar",
        "/drafts",
        "/publishing",
        "/opportunities",
        "/leads",
        "/analytics",
        "/settings",
        "/logs",
        "/health",
    ]:
        response = client.get(path)
        assert response.status_code == 200, path
    missing = client.get("/does-not-exist")
    assert missing.status_code == 404
    assert "does not exist" in missing.text
    bad = client.post("/drafts", data={"count": "0", "platform": "instagram"}, follow_redirects=True)
    assert bad.status_code == 400
    assert "Nothing was generated" in bad.text
    down = client.post(
        "/drafts",
        data={"count": "1", "platform": "instagram", "categories": "korean_learner_mistakes"},
        follow_redirects=True,
    )
    assert "No post was generated" in down.text
    assert "been-vs-gone" not in down.text
    submit = client.post("/opportunities/1/submit")
    assert submit.status_code == 404
    marked = client.post("/publishing/1/mark-published")
    assert marked.status_code == 404
    analytics = client.get("/analytics")
    assert "not available" in analytics.text
    assert "No drafts yet for Common Korean learner mistakes." in analytics.text


def test_generate_edit_queue_and_manual_blog_page(tmp_path):
    server = FakeOllama().start()
    server.push(valid_draft(platform="naver_blog"))
    server.push(valid_draft(topic_key="edited-topic", platform="naver_blog", hook="A better hook for been and gone."))
    try:
        settings = make_settings(tmp_path, ollama_host=server.host, ollama_timeout=5)
        app = create_app(settings)
        with TestClient(app) as client:
            created = client.post(
                "/drafts",
                data={"count": "1", "platform": "naver_blog", "categories": "korean_learner_mistakes"},
                follow_redirects=True,
            )
            assert "been-vs-gone" in created.text
            detail = client.get("/drafts/1")
            assert detail.status_code == 200
            saved = client.post(
                "/drafts/1/save",
                data={
                    "topic_key": "been-vs-gone",
                    "category": "korean_learner_mistakes",
                    "platform": "naver_blog",
                    "hook": "Been and gone are not the same trip.",
                    "english_examples": "I have been to Busan.\nI have gone to Busan.",
                    "korean_explanation": "한국 학습자는 이 둘을 자주 섞습니다. 경험과 현재 위치는 다릅니다.",
                    "engagement_question": "Which one did you mean?",
                    "cta": "",
                    "hashtags": "#English\n#영어공부",
                    "uncertainty_reason": "",
                },
                follow_redirects=True,
            )
            assert "Saved" in saved.text
            client.post("/drafts/1/approve", follow_redirects=True)
            queued = client.post("/drafts/1/queue", follow_redirects=True)
            assert queued.status_code == 200
            assert "2020" in queued.text
            assert "Publish via official API" not in queued.text
            refused = client.post("/publishing/1/publish", follow_redirects=True)
            assert "Nothing was published" in refused.text
            assert "published</strong>" not in refused.text.lower() or "manual-action-required" in refused.text
            export = client.get("/publishing/1/export")
            assert export.status_code == 200
            assert "#영어공부" in export.text
            regenerated = client.post("/drafts/1/regenerate", follow_redirects=True)
            assert "edited-topic" in regenerated.text
    finally:
        server.stop()


def test_settings_do_not_echo_tokens_and_backup_restore(tmp_path):
    settings = make_settings(tmp_path, facebook_page_token="SUPERSECRETTOKEN", facebook_page_id="42")
    app = create_app(settings)
    with TestClient(app) as client:
        page = client.get("/settings")
        assert "SUPERSECRETTOKEN" not in page.text
        assert "configured" in page.text
        client.post(
            "/leads",
            data={
                "display_name": "Mina",
                "source": "friend",
                "inquiry_date": "2026-10-25",
                "learning_goal": "Interviews",
                "consent": "1",
                "communication_preference": "email",
                "trial_lesson_status": "not_offered",
            },
            follow_redirects=True,
        )
        client.post("/backups", follow_redirects=True)
        client.post(
            "/leads",
            data={
                "display_name": "Jun",
                "source": "website",
                "inquiry_date": "2026-10-25",
                "learning_goal": "Meetings",
                "consent": "1",
                "communication_preference": "phone",
                "trial_lesson_status": "offered",
            },
            follow_redirects=True,
        )
        refused = client.post("/backups/restore", data={"name": "nope.db"}, follow_redirects=True)
        assert "Nothing was restored" in refused.text
        backups = client.get("/logs").text
        assert "studio-" in backups
        match = re.search(r'href="/backups/(studio-[^"]+\.db)"', backups)
        assert match is not None
        name = match.group(1)
        restored = client.post(
            "/backups/restore",
            data={"name": name, "confirm_restore": "1"},
            follow_redirects=True,
        )
        assert "Restored" in restored.text
        leads = client.get("/leads").text
        assert "friend" in leads
        assert "website" not in leads


def test_opportunity_import_and_lead_follow_up_copy(client):
    imported = client.post(
        "/opportunities/import",
        data={"url": "https://example.com/club", "topic": "Interview tips", "reply_language": "en", "excerpt": ""},
        follow_redirects=True,
    )
    assert "Nothing was sent" in imported.text
    assert "not fetched" in imported.text
    saved = client.post(
        "/leads",
        data={
            "source": "kakao",
            "inquiry_date": "2026-10-25",
            "learning_goal": "Pronunciation",
            "communication_preference": "none",
            "trial_lesson_status": "declined",
            "follow_up_on": "2026-12-01",
        },
        follow_redirects=True,
    )
    assert "not marked due" in saved.text
    assert "2026-12-01" not in client.get("/leads").text
