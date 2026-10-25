import json

import pytest
from pydantic import ValidationError

from app.content.ollama import OllamaClient, OllamaUnavailable
from app.content.schema import CATEGORY_LABELS, DraftContent
from app.content.service import (
    GenerationError,
    approve_draft,
    generate_and_save,
    generate_batch,
    list_drafts,
    list_slots,
    regenerate_draft,
    save_edit,
    save_slot,
)
from app.db import connect, init_db
from tests.helpers import make_settings, valid_draft
from tests.ollama_server import FakeOllama


def test_category_labels_match_schema():
    allowed = set(DraftContent.model_fields["category"].annotation.__args__)
    assert allowed == set(CATEGORY_LABELS)


def test_messy_topic_key_becomes_a_slug():
    model = DraftContent.model_validate(valid_draft(topic_key="Insta Practical English!"))
    assert model.topic_key == "insta-practical-english"
    from_topic = valid_draft(topic_key="!!!", hook="Been and gone are not the same trip.")
    from_topic["topic"] = "Present Perfect Time"
    assert DraftContent.model_validate(from_topic).topic_key == "present-perfect-time"
    from_hook = valid_draft(topic_key="---", hook="Been and gone are not the same trip.")
    assert DraftContent.model_validate(from_hook).topic_key == "been-and-gone-are-not-the-same-trip"
    with pytest.raises(ValidationError):
        DraftContent.model_validate({"topic_key": "Insta Practical English!"})


def test_fake_ollama_generates_draft_and_sends_json_format(tmp_path):
    server = FakeOllama().start()
    server.push(valid_draft())
    try:
        settings = make_settings(tmp_path, ollama_host=server.host)
        conn = connect(settings.database_path)
        init_db(conn)
        client = OllamaClient(server.host, "qwen2.5:7b", timeout=5)
        draft = generate_and_save(
            conn,
            client,
            category="korean_learner_mistakes",
            platform="instagram",
            attempts=3,
        )
        conn.commit()
        assert draft["content_status"] == "draft"
        assert draft["topic_key"] == "been-vs-gone"
        assert "한국" in draft["korean_explanation"]
        body = server.requests[0]
        assert body["format"] == "json"
        assert body["stream"] is False
        assert body["model"] == "qwen2.5:7b"
        assert "been-vs-gone" not in json.dumps(body) or "recent_topic_keys" in json.dumps(body)
        assert "korean_learner_mistakes" in json.dumps(body)
    finally:
        server.stop()


def test_invalid_output_is_retried_then_saved(tmp_path):
    server = FakeOllama().start()
    server.push("not json")
    server.push(valid_draft(topic_key="articles-a-an"))
    try:
        settings = make_settings(tmp_path, ollama_host=server.host)
        conn = connect(settings.database_path)
        init_db(conn)
        draft = generate_and_save(
            conn,
            OllamaClient(server.host, "qwen2.5:7b", timeout=5),
            category="korean_learner_mistakes",
            platform="instagram",
            attempts=3,
        )
        assert draft["topic_key"] == "articles-a-an"
        assert len(server.requests) == 2
        assert "fix_previous_output" in server.requests[1]["messages"][1]["content"]
    finally:
        server.stop()


def test_all_invalid_saves_nothing(tmp_path):
    server = FakeOllama().start()
    server.push("nope")
    server.push({"topic_key": "Bad Key"})
    server.push(valid_draft(korean_explanation="English only explanation here."))
    try:
        settings = make_settings(tmp_path, ollama_host=server.host)
        conn = connect(settings.database_path)
        init_db(conn)
        with pytest.raises(GenerationError, match="No post was saved"):
            generate_and_save(
                conn,
                OllamaClient(server.host, "qwen2.5:7b", timeout=5),
                category="korean_learner_mistakes",
                platform="instagram",
                attempts=3,
            )
        assert list_drafts(conn) == []
    finally:
        server.stop()


def test_ollama_down_does_not_invent_a_draft(tmp_path):
    settings = make_settings(tmp_path, ollama_host="http://127.0.0.1:9", ollama_timeout=1)
    conn = connect(settings.database_path)
    init_db(conn)
    with pytest.raises(OllamaUnavailable, match="No post was generated"):
        generate_and_save(
            conn,
            OllamaClient(settings.ollama_host, settings.ollama_model, timeout=1),
            category="korean_learner_mistakes",
            platform="instagram",
            attempts=3,
        )
    assert list_drafts(conn) == []


def test_model_error_is_not_retried_as_a_draft(tmp_path):
    server = FakeOllama().start()
    server.push({"__http__": 200, "__body__": {"error": "model 'qwen2.5:7b' not found"}})
    try:
        settings = make_settings(tmp_path, ollama_host=server.host)
        conn = connect(settings.database_path)
        init_db(conn)
        with pytest.raises(OllamaUnavailable, match="No post was generated"):
            generate_and_save(
                conn,
                OllamaClient(server.host, "qwen2.5:7b", timeout=5),
                category="korean_learner_mistakes",
                platform="instagram",
                attempts=3,
            )
        assert len(server.requests) == 1
        assert list_drafts(conn) == []
    finally:
        server.stop()


def test_recent_topic_is_rejected_and_sent_back(tmp_path):
    server = FakeOllama().start()
    server.push(valid_draft())
    server.push(valid_draft())
    server.push(valid_draft(topic_key="present-perfect-time"))
    try:
        settings = make_settings(tmp_path, ollama_host=server.host)
        conn = connect(settings.database_path)
        init_db(conn)
        client = OllamaClient(server.host, "qwen2.5:7b", timeout=5)
        first = generate_and_save(
            conn, client, category="korean_learner_mistakes", platform="instagram", attempts=3
        )
        second = generate_and_save(
            conn, client, category="korean_learner_mistakes", platform="instagram", attempts=3
        )
        assert first["topic_key"] == "been-vs-gone"
        assert second["topic_key"] == "present-perfect-time"
        retry_prompt = server.requests[-1]["messages"][1]["content"]
        assert "been-vs-gone" in retry_prompt
        assert "repeats a recent topic" in retry_prompt
    finally:
        server.stop()


def test_uncertain_claim_blocks_approval_until_reviewed(tmp_path):
    server = FakeOllama().start()
    server.push(
        valid_draft(
            topic_key="maybe-would",
            uncertain_linguistic_claim=True,
            uncertainty_reason="The politeness ranking of would and could is context dependent.",
        )
    )
    try:
        settings = make_settings(tmp_path, ollama_host=server.host)
        conn = connect(settings.database_path)
        init_db(conn)
        draft = generate_and_save(
            conn,
            OllamaClient(server.host, "qwen2.5:7b", timeout=5),
            category="korean_learner_mistakes",
            platform="instagram",
            attempts=2,
        )
        assert draft["content_status"] == "draft"
        assert draft["review_required"] is True
        with pytest.raises(GenerationError, match="Nothing was approved"):
            approve_draft(conn, draft["id"], human_reviewed=False)
        approved = approve_draft(conn, draft["id"], human_reviewed=True)
        assert approved["content_status"] == "approved"
        assert approved["human_reviewed"] is True
    finally:
        server.stop()


def test_batch_round_robin_and_edit_regenerate(tmp_path):
    server = FakeOllama().start()
    server.push(valid_draft(topic_key="batch-one", category="korean_learner_mistakes", platform="facebook"))
    server.push(valid_draft(topic_key="batch-two", category="interview_prep", platform="facebook"))
    server.push(valid_draft(topic_key="batch-three", category="korean_learner_mistakes", platform="facebook"))
    server.push(
        valid_draft(topic_key="batch-one-rewritten", category="korean_learner_mistakes", platform="facebook")
    )
    try:
        settings = make_settings(tmp_path, ollama_host=server.host)
        conn = connect(settings.database_path)
        init_db(conn)
        client = OllamaClient(server.host, "qwen2.5:7b", timeout=5)
        created, note = generate_batch(
            conn,
            client,
            count=3,
            categories=["korean_learner_mistakes", "interview_prep"],
            platform="facebook",
            attempts=2,
        )
        assert note == ""
        assert [row["category"] for row in created] == [
            "korean_learner_mistakes",
            "interview_prep",
            "korean_learner_mistakes",
        ]
        assert all(row["platform"] == "facebook" for row in created)
        edited = save_edit(
            conn,
            created[0]["id"],
            valid_draft(
                topic_key="batch-one",
                platform="facebook",
                hook="A clearer hook for the same mistake.",
            ),
        )
        assert edited["content_status"] == "draft"
        assert edited["hook"].startswith("A clearer")
        regenerated = regenerate_draft(conn, client, created[0]["id"], attempts=2)
        assert regenerated["topic_key"] == "batch-one-rewritten"
        assert regenerated["id"] == created[0]["id"]
        filtered = list_drafts(conn, category="interview_prep", status="draft")
        assert len(filtered) == 1
    finally:
        server.stop()


def test_calendar_save_does_not_need_ollama(db):
    before = len(list_slots(db))
    save_slot(
        db,
        weekday=0,
        category="grammar_quizzes",
        platform="facebook",
        count=2,
        enabled=False,
        notes="quiz day",
    )
    db.commit()
    slots = list_slots(db)
    assert len(slots) == before + 1
    assert any(slot["notes"] == "quiz day" and slot["enabled"] == 0 for slot in slots)
