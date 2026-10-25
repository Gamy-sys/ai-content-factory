import re
import sqlite3
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

import httpx

from app.clock import utc_now_iso
from app.db import get_setting, set_setting

PUBLIC_RSS = [
    {
        "id": "bbc-6-minute-english",
        "name": "BBC Learning English — 6 Minute English",
        "url": "https://feeds.bbci.co.uk/learningenglish/english/features/6-minute-english/rss",
        "reason": (
            "This is a public podcast feed, not a comment box. "
            "Use it as a lesson idea. Do not post a promotional reply on BBC."
        ),
    },
    {
        "id": "voa-learning-english",
        "name": "VOA Learning English Podcast",
        "url": "https://learningenglish.voanews.com/podcast/?count=20&zoneId=1689",
        "reason": (
            "This is a public podcast feed, not a comment box. "
            "Use it as a lesson idea. Do not post a promotional reply on VOA."
        ),
    },
]
ALLOWED_FEED_URLS = {item["url"] for item in PUBLIC_RSS}
_KEYWORDS = ("english", "grammar", "vocabulary", "pronunciation", "idiom", "phrase", "lesson", "speaking")


class ResearchError(RuntimeError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _strip_tags(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return re.sub(r"\s+", " ", text).strip()


def parse_rss(xml_text: str) -> list[dict]:
    if not xml_text or not xml_text.strip():
        raise ResearchError("The feed was empty.")
    if "<!DOCTYPE" in xml_text.upper():
        raise ResearchError("The feed was refused because it declared a doctype.")
    if len(xml_text) > 2_000_000:
        raise ResearchError("The feed was too large and was ignored.")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ResearchError("The feed was not valid XML.") from exc
    items: list[dict] = []
    for node in root.iter():
        if _local(node.tag) != "item":
            continue
        title = ""
        link = ""
        description = ""
        for child in list(node):
            name = _local(child.tag)
            text = (child.text or "").strip()
            if name == "title":
                title = _strip_tags(text)
            elif name == "link":
                link = text
            elif name == "description":
                description = _strip_tags(text)
        if link:
            items.append({"title": title or link, "link": link, "description": description})
    return items


def relevance_for(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    hits = sum(1 for word in _KEYWORDS if word in text)
    if hits >= 2:
        return "high"
    if hits == 1:
        return "medium"
    return "low"


def suggested_reply(title: str, description: str, language: str) -> str:
    note = (description or "").strip()[:400]
    if language == "en":
        return (
            "Draft for the teacher only. This was not sent anywhere.\n\n"
            f"Topic: {title}\n"
            f"Notes: {note}\n\n"
            "Use this as a lesson angle for Korean adults. "
            "Do not post a promotional reply unless you have checked that community's rules."
        )
    return (
        "선생님용 초안입니다. 어디에도 전송하지 않았습니다.\n\n"
        f"주제: {title}\n"
        f"메모: {note}\n\n"
        "한국 성인 학습자를 위한 수업 각도로만 쓰세요. "
        "커뮤니티 규칙을 확인하기 전에는 홍보 댓글을 올리지 마세요."
    )


def _validate_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResearchError("URL must start with http:// or https://.")
    return url.strip()


def _row(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["uncertain_rules"] = bool(data["uncertain_rules"])
    return data


def get_opportunity(conn: sqlite3.Connection, opportunity_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
    return None if row is None else _row(row)


def list_opportunities(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM opportunities ORDER BY id DESC").fetchall()
    return [_row(row) for row in rows]


def _insert(
    conn: sqlite3.Connection,
    *,
    url: str,
    source: str,
    topic: str,
    relevance: str,
    reply: str,
    language: str,
    reason: str,
    via: str,
    excerpt: str,
) -> dict:
    now = utc_now_iso()
    try:
        cursor = conn.execute(
            """
            INSERT INTO opportunities (
                url, source, discovered_at, topic, relevance, suggested_reply, reply_language,
                uncertain_rules, uncertain_reason, approval_status, imported_via, excerpt, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 'pending', ?, ?, ?)
            """,
            (url, source, now, topic, relevance, reply, language, reason, via, excerpt[:1000], now),
        )
    except sqlite3.IntegrityError as exc:
        raise ResearchError("That URL is already in the list. Nothing new was imported.") from exc
    item = get_opportunity(conn, int(cursor.lastrowid))
    if item is None:
        raise ResearchError("The opportunity could not be read back.")
    return item


def import_manual(conn: sqlite3.Connection, *, url: str, topic: str, excerpt: str, reply_language: str) -> dict:
    clean_url = _validate_url(url)
    title = topic.strip()
    if len(title) < 3:
        raise ResearchError("Topic is required.")
    if reply_language not in {"ko", "en"}:
        raise ResearchError("Reply language must be Korean or English.")
    reply = suggested_reply(title, excerpt, reply_language)
    if not excerpt.strip():
        reply += "\n\nThe page was not fetched. Read it yourself before using this note."
    return _insert(
        conn,
        url=clean_url,
        source="manual",
        topic=title,
        relevance=relevance_for(title, excerpt),
        reply=reply,
        language=reply_language,
        reason="Promotional and community rules for this URL were not verified. Do not post until you have checked them.",
        via="manual",
        excerpt=excerpt.strip(),
    )


def import_feed_items(conn: sqlite3.Connection, source: dict, items: list[dict]) -> tuple[int, int]:
    created = 0
    duplicates = 0
    for item in items[:10]:
        link = (item.get("link") or "").strip()
        try:
            link = _validate_url(link)
        except ResearchError:
            continue
        exists = conn.execute("SELECT id FROM opportunities WHERE url = ?", (link,)).fetchone()
        if exists:
            duplicates += 1
            continue
        title = item.get("title") or link
        description = item.get("description") or ""
        _insert(
            conn,
            url=link,
            source=source["name"],
            topic=title,
            relevance=relevance_for(title, description),
            reply=suggested_reply(title, description, "ko"),
            language="ko",
            reason=source["reason"],
            via="rss",
            excerpt=description,
        )
        created += 1
    return created, duplicates


def set_approval(conn: sqlite3.Connection, opportunity_id: int, status: str) -> dict:
    if status not in {"approved", "rejected"}:
        raise ResearchError("Unknown decision.")
    current = get_opportunity(conn, opportunity_id)
    if current is None:
        raise ResearchError("Opportunity not found.")
    conn.execute(
        "UPDATE opportunities SET approval_status = ? WHERE id = ?",
        (status, opportunity_id),
    )
    updated = get_opportunity(conn, opportunity_id)
    if updated is None:
        raise ResearchError("Opportunity not found.")
    return updated


def run_research_job(conn: sqlite3.Connection, fetcher) -> dict:
    if get_setting(conn, "job_research_rss") != "1":
        return {"ran": False, "reason": "disabled", "created": 0, "duplicates": 0, "errors": []}
    created = 0
    duplicates = 0
    errors: list[str] = []
    for source in PUBLIC_RSS:
        try:
            xml_text = fetcher(source["url"])
            items = parse_rss(xml_text)
            added, skipped = import_feed_items(conn, source, items)
            created += added
            duplicates += skipped
        except Exception as exc:
            errors.append(f"{source['id']}: {exc}")
    status = f"created {created}" if not errors else f"created {created}; {'; '.join(errors)[:240]}"
    set_setting(conn, "job_research_rss_last_run", utc_now_iso())
    set_setting(conn, "job_research_rss_last_status", status)
    return {"ran": True, "created": created, "duplicates": duplicates, "errors": errors}


def fetch_allowlisted(url: str, timeout: float = 20.0) -> str:
    if url not in ALLOWED_FEED_URLS:
        raise ResearchError(
            "Automatic fetch is refused for this URL. Use manual import. Nothing was downloaded."
        )
    try:
        response = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": "local-english-studio/1.0"},
        )
    except httpx.HTTPError as exc:
        raise ResearchError(f"The public feed could not be read. ({exc.__class__.__name__})") from exc
    if response.status_code != 200:
        raise ResearchError(f"The public feed returned HTTP {response.status_code}.")
    if len(response.text) > 2_000_000:
        raise ResearchError("The feed was too large and was ignored.")
    return response.text
