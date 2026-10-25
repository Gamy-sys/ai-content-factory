from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.content.ollama import OllamaClient
from app.db import get_setting, set_setting
from app.main import create_app
from app.publishing.service import list_items
from app.scheduler import run_calendar_job
from tests.helpers import make_settings, valid_draft
from tests.ollama_server import FakeOllama


class ExplodingOllama:
    def chat_json(self, messages):
        raise AssertionError("Ollama was called while the job was off")


def test_calendar_job_is_off_by_default(db, settings):
    assert get_setting(db, "job_weekly_calendar") == "0"
    result = run_calendar_job(db, settings, ExplodingOllama())
    assert result["ran"] is False
    assert result["created"] == []


def test_enabled_calendar_job_creates_drafts_only(tmp_path):
    server = FakeOllama().start()
    server.push(valid_draft())
    try:
        settings = make_settings(tmp_path, ollama_host=server.host, generation_attempts=2)
        from app.db import connect, init_db

        conn = connect(settings.database_path)
        init_db(conn)
        set_setting(conn, "job_weekly_calendar", "1")
        monday = datetime(2026, 10, 26, 8, 0, tzinfo=ZoneInfo("Asia/Seoul"))
        result = run_calendar_job(
            conn,
            settings,
            OllamaClient(server.host, "qwen2.5:7b", timeout=5),
            now=monday,
        )
        conn.commit()
        assert result["ran"] is True
        assert result["error"] == ""
        assert len(result["created"]) == 1
        assert list_items(conn) == []
        row = conn.execute("SELECT content_status, calendar_slot_id, category FROM drafts").fetchone()
        assert row["content_status"] == "draft"
        assert row["category"] == "korean_learner_mistakes"
        assert row["calendar_slot_id"] is not None
    finally:
        server.stop()


def test_scheduler_starts_paused_and_can_resume(tmp_path):
    settings = make_settings(tmp_path, start_scheduler=True)
    app = create_app(settings)
    with TestClient(app) as client:
        job = app.state.scheduler.get_job("weekly_calendar")
        assert job.next_run_time is None
        health = client.get("/health").json()
        assert health["bind"] == "127.0.0.1"
        assert health["jobs"]["weekly_calendar"] is False
        response = client.post(
            "/settings",
            data={"job_weekly_calendar": "1", "ollama_host": "http://127.0.0.1:11434", "ollama_model": "qwen2.5:7b"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert app.state.scheduler.get_job("weekly_calendar").next_run_time is not None
        assert client.get("/health").json()["jobs"]["research_rss"] is False
