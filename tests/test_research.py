import inspect

import pytest

import app.research.service as research
from app.db import get_setting
from app.research.service import (
    ResearchError,
    fetch_allowlisted,
    import_manual,
    parse_rss,
    relevance_for,
    run_research_job,
    set_approval,
)


SAMPLE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>English grammar in the news</title>
      <link>https://example.com/grammar-lesson</link>
      <description>A short vocabulary lesson for learners.</description>
    </item>
    <item>
      <title>Weather</title>
      <link>https://example.com/weather</link>
      <description>Rain tomorrow.</description>
    </item>
  </channel>
</rss>
"""


def test_parse_relevance_and_manual_import(db):
    items = parse_rss(SAMPLE)
    assert items[0]["title"] == "English grammar in the news"
    assert relevance_for(items[0]["title"], items[0]["description"]) == "high"
    saved = import_manual(
        db,
        url="https://example.com/study-group",
        topic="Workplace small talk",
        excerpt="",
        reply_language="ko",
    )
    db.commit()
    assert saved["approval_status"] == "pending"
    assert saved["uncertain_rules"] is True
    assert "전송하지 않았습니다" in saved["suggested_reply"]
    assert "was not fetched" in saved["suggested_reply"]
    with pytest.raises(ResearchError, match="already"):
        import_manual(
            db,
            url="https://example.com/study-group",
            topic="Workplace small talk",
            excerpt="notes",
            reply_language="en",
        )
    with pytest.raises(ResearchError):
        import_manual(db, url="javascript:alert(1)", topic="bad", excerpt="", reply_language="ko")


def test_rss_job_and_approval_do_not_submit(db):
    assert get_setting(db, "job_research_rss") == "0"
    called = {"n": 0}

    def fetcher(url):
        called["n"] += 1
        return SAMPLE

    skipped = run_research_job(db, fetcher)
    assert skipped["ran"] is False
    assert called["n"] == 0
    from app.db import set_setting

    set_setting(db, "job_research_rss", "1")
    first = run_research_job(db, fetcher)
    second = run_research_job(db, fetcher)
    db.commit()
    assert first["created"] == 2
    assert first["duplicates"] == 2
    assert second["duplicates"] == 4
    row = db.execute("SELECT id, uncertain_rules FROM opportunities LIMIT 1").fetchone()
    approved = set_approval(db, row["id"], "approved")
    assert approved["approval_status"] == "approved"
    assert approved["uncertain_rules"] == 1
    names = [name for name in dir(research) if not name.startswith("_")]
    assert not any("submit" in name or name.startswith("send") for name in names)
    assert "submit" not in inspect.getsource(research)


def test_unlisted_fetch_does_not_call_http(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("network")

    monkeypatch.setattr(research.httpx, "get", boom)
    with pytest.raises(ResearchError, match="Nothing was downloaded"):
        fetch_allowlisted("https://example.com/private")
