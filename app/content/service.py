import json
import sqlite3

from pydantic import ValidationError

from app.clock import utc_now_iso
from app.config import Settings
from app.content.ollama import OllamaBadResponse, OllamaClient, OllamaUnavailable
from app.content.schema import CATEGORY_LABELS, CATEGORIES, PLATFORM_LABELS, DraftContent
from app.db import get_setting

SYSTEM_PROMPT = (
    "You write short social posts for Korean adults learning practical English. "
    "Return one JSON object and nothing else. "
    "If a grammar or usage claim is not solid, set uncertain_linguistic_claim to true and explain why. "
    "Do not say the post has been published."
)


class GenerationError(RuntimeError):
    pass


def effective_ollama(conn: sqlite3.Connection, settings: Settings) -> tuple[str, str]:
    host = (get_setting(conn, "ollama_host") or "").strip() or settings.ollama_host
    model = (get_setting(conn, "ollama_model") or "").strip() or settings.ollama_model
    return host.rstrip("/"), model


def recent_topic_keys(conn: sqlite3.Connection, limit: int = 30) -> list[str]:
    rows = conn.execute(
        "SELECT topic_key FROM topic_history ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [str(row["topic_key"]) for row in rows]


def _draft_from_row(row: sqlite3.Row) -> dict:
    carousel = row["carousel_outline_json"]
    return {
        "id": row["id"],
        "topic_key": row["topic_key"],
        "category": row["category"],
        "platform": row["platform"],
        "hook": row["hook"],
        "english_examples": json.loads(row["english_examples_json"]),
        "korean_explanation": row["korean_explanation"],
        "engagement_question": row["engagement_question"],
        "cta": row["cta"],
        "hashtags": json.loads(row["hashtags_json"]),
        "carousel_outline": json.loads(carousel) if carousel else None,
        "content_status": row["content_status"],
        "review_required": bool(row["review_required"]),
        "review_reason": row["review_reason"] or "",
        "human_reviewed": bool(row["human_reviewed"]),
        "uncertain_linguistic_claim": bool(row["uncertain_linguistic_claim"]),
        "generation_attempts": row["generation_attempts"],
        "calendar_slot_id": row["calendar_slot_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_draft(conn: sqlite3.Connection, draft_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    return None if row is None else _draft_from_row(row)


def list_drafts(conn: sqlite3.Connection, category: str | None = None, status: str | None = None) -> list[dict]:
    sql = "SELECT * FROM drafts WHERE 1=1"
    args: list[object] = []
    if category:
        sql += " AND category = ?"
        args.append(category)
    if status:
        sql += " AND content_status = ?"
        args.append(status)
    sql += " ORDER BY id DESC"
    return [_draft_from_row(row) for row in conn.execute(sql, args)]


def _messages(category: str, platform: str, recent: list[str], previous_error: str) -> list[dict]:
    shape: dict = {
        "topic_key": "lowercase-hyphenated-slug",
        "category": category,
        "platform": platform,
        "hook": "one or two sentences",
        "english_examples": ["natural example", "second example"],
        "korean_explanation": "자연스러운 한국어 설명",
        "engagement_question": "A question for the reader?",
        "cta": "",
        "hashtags": ["#English", "#영어공부", "#KoreanLearners"],
        "carousel_outline": ["slide 1", "slide 2"] if category == "educational_posts" else None,
        "uncertain_linguistic_claim": False,
        "uncertainty_reason": "",
    }
    payload = {
        "task": "Write one new post for Korean adults learning practical English.",
        "category": category,
        "category_label": CATEGORY_LABELS[category],
        "platform": platform,
        "audience": [
            "common Korean learner mistakes",
            "natural conversational expressions",
            "business and workplace English",
            "interview prep",
            "grammar quizzes and interactive questions",
            "pronunciation and speaking confidence",
            "short educational posts and carousel outlines",
        ],
        "rules": [
            "category and platform must match the requested values exactly",
            "topic_key must not be one of recent_topic_keys",
            "korean_explanation must be Korean",
            "english_examples must be natural English",
            "flag uncertain linguistic claims instead of stating them as fact",
        ],
        "recent_topic_keys": recent,
        "json_shape": shape,
    }
    if previous_error:
        payload["fix_previous_output"] = previous_error[:800]
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _validation_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = []
        for error in exc.errors():
            loc = ".".join(str(item) for item in error.get("loc", []))
            parts.append(f"{loc}: {error.get('msg')}")
        return "; ".join(parts)[:800]
    return str(exc)[:800]


def produce_draft(
    ollama: OllamaClient,
    *,
    category: str,
    platform: str,
    blocked_keys: set[str],
    attempts: int,
) -> tuple[DraftContent, int, dict]:
    if category not in CATEGORY_LABELS:
        raise GenerationError("Unknown category. Nothing was generated.")
    previous = ""
    last = "no response"
    for attempt in range(1, attempts + 1):
        messages = _messages(category, platform, sorted(blocked_keys), previous)
        try:
            raw = ollama.chat_json(messages)
        except OllamaUnavailable:
            raise
        except OllamaBadResponse as exc:
            last = str(exc)
            previous = last
            continue
        try:
            model = DraftContent.model_validate(raw)
        except ValidationError as exc:
            last = _validation_message(exc)
            previous = last
            continue
        if model.category != category or model.platform != platform:
            last = f"category must be {category} and platform must be {platform}"
            previous = last
            continue
        if model.topic_key in blocked_keys:
            last = f"topic_key repeats a recent topic: {model.topic_key}"
            previous = last
            continue
        return model, attempt, raw
    raise GenerationError(
        f"Ollama returned unusable drafts after {attempts} attempts. No post was saved. Last problem: {last}"
    )


def _insert_topic(conn: sqlite3.Connection, model: DraftContent, draft_id: int) -> None:
    conn.execute(
        """
        INSERT INTO topic_history (topic_key, category, draft_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (model.topic_key, model.category, draft_id, utc_now_iso()),
    )


def _persist_values(model: DraftContent, raw: dict, attempts: int) -> tuple:
    now = utc_now_iso()
    return (
        model.topic_key,
        model.category,
        model.platform,
        model.hook,
        json.dumps(model.english_examples, ensure_ascii=False),
        model.korean_explanation,
        model.engagement_question,
        model.cta,
        json.dumps(model.hashtags, ensure_ascii=False),
        json.dumps(model.carousel_outline, ensure_ascii=False) if model.carousel_outline else None,
        "draft",
        1 if model.uncertain_linguistic_claim else 0,
        model.uncertainty_reason.strip() if model.uncertain_linguistic_claim else "",
        0,
        1 if model.uncertain_linguistic_claim else 0,
        attempts,
        json.dumps(raw, ensure_ascii=False),
        now,
    )


def save_new_draft(
    conn: sqlite3.Connection,
    model: DraftContent,
    raw: dict,
    attempts: int,
    calendar_slot_id: int | None = None,
) -> dict:
    values = _persist_values(model, raw, attempts)
    now = values[-1]
    cursor = conn.execute(
        """
        INSERT INTO drafts (
            topic_key, category, platform, hook, english_examples_json, korean_explanation,
            engagement_question, cta, hashtags_json, carousel_outline_json, content_status,
            review_required, review_reason, human_reviewed, uncertain_linguistic_claim,
            generation_attempts, raw_json, updated_at, calendar_slot_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (*values, calendar_slot_id, now),
    )
    draft_id = int(cursor.lastrowid)
    _insert_topic(conn, model, draft_id)
    draft = get_draft(conn, draft_id)
    if draft is None:
        raise GenerationError("The draft could not be read back. Nothing else was published.")
    return draft


def generate_and_save(
    conn: sqlite3.Connection,
    ollama: OllamaClient,
    *,
    category: str,
    platform: str,
    attempts: int,
    calendar_slot_id: int | None = None,
) -> dict:
    blocked = set(recent_topic_keys(conn))
    model, used, raw = produce_draft(
        ollama,
        category=category,
        platform=platform,
        blocked_keys=blocked,
        attempts=attempts,
    )
    return save_new_draft(conn, model, raw, used, calendar_slot_id)


def generate_batch(
    conn: sqlite3.Connection,
    ollama: OllamaClient,
    *,
    count: int,
    categories: list[str],
    platform: str,
    attempts: int,
) -> tuple[list[dict], str]:
    if not 1 <= count <= 10:
        raise GenerationError("Count must be between 1 and 10. Nothing was generated.")
    if not categories or any(item not in CATEGORY_LABELS for item in categories):
        raise GenerationError("Choose at least one real category. Nothing was generated.")
    created: list[dict] = []
    note = ""
    for index in range(count):
        category = categories[index % len(categories)]
        try:
            created.append(
                generate_and_save(
                    conn,
                    ollama,
                    category=category,
                    platform=platform,
                    attempts=attempts,
                )
            )
        except OllamaUnavailable:
            raise
        except GenerationError as exc:
            note = str(exc)
            break
    return created, note


def save_edit(conn: sqlite3.Connection, draft_id: int, payload: dict) -> dict:
    current = get_draft(conn, draft_id)
    if current is None:
        raise GenerationError("Draft not found.")
    try:
        model = DraftContent.model_validate(payload)
    except ValidationError as exc:
        raise GenerationError(_validation_message(exc)) from exc
    owner = conn.execute(
        "SELECT draft_id FROM topic_history WHERE topic_key = ?",
        (model.topic_key,),
    ).fetchone()
    if owner is not None and owner["draft_id"] not in (None, draft_id):
        raise GenerationError("That topic key is already used by another draft. Nothing was saved.")
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE drafts SET
            topic_key=?, category=?, platform=?, hook=?, english_examples_json=?,
            korean_explanation=?, engagement_question=?, cta=?, hashtags_json=?,
            carousel_outline_json=?, content_status='draft', review_required=?,
            review_reason=?, human_reviewed=0, uncertain_linguistic_claim=?,
            raw_json=?, updated_at=?
        WHERE id=?
        """,
        (
            model.topic_key,
            model.category,
            model.platform,
            model.hook,
            json.dumps(model.english_examples, ensure_ascii=False),
            model.korean_explanation,
            model.engagement_question,
            model.cta,
            json.dumps(model.hashtags, ensure_ascii=False),
            json.dumps(model.carousel_outline, ensure_ascii=False) if model.carousel_outline else None,
            1 if model.uncertain_linguistic_claim else 0,
            model.uncertainty_reason.strip() if model.uncertain_linguistic_claim else "",
            1 if model.uncertain_linguistic_claim else 0,
            json.dumps(model.model_dump(), ensure_ascii=False),
            now,
            draft_id,
        ),
    )
    if current["topic_key"] != model.topic_key:
        conn.execute("DELETE FROM topic_history WHERE topic_key = ? AND draft_id = ?", (current["topic_key"], draft_id))
    conn.execute(
        """
        INSERT INTO topic_history (topic_key, category, draft_id, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(topic_key) DO UPDATE SET category=excluded.category, draft_id=excluded.draft_id
        """,
        (model.topic_key, model.category, draft_id, now),
    )
    updated = get_draft(conn, draft_id)
    if updated is None:
        raise GenerationError("Draft not found after save.")
    return updated


def regenerate_draft(
    conn: sqlite3.Connection,
    ollama: OllamaClient,
    draft_id: int,
    attempts: int,
) -> dict:
    current = get_draft(conn, draft_id)
    if current is None:
        raise GenerationError("Draft not found.")
    blocked = set(recent_topic_keys(conn))
    blocked.add(current["topic_key"])
    model, used, raw = produce_draft(
        ollama,
        category=current["category"],
        platform=current["platform"],
        blocked_keys=blocked,
        attempts=attempts,
    )
    saved = save_edit(conn, draft_id, model.model_dump())
    conn.execute(
        "UPDATE drafts SET generation_attempts=?, raw_json=?, updated_at=? WHERE id=?",
        (used, json.dumps(raw, ensure_ascii=False), utc_now_iso(), draft_id),
    )
    updated = get_draft(conn, draft_id)
    return updated or saved


def approve_draft(conn: sqlite3.Connection, draft_id: int, human_reviewed: bool) -> dict:
    draft = get_draft(conn, draft_id)
    if draft is None:
        raise GenerationError("Draft not found.")
    if draft["review_required"] and not human_reviewed:
        raise GenerationError(
            "This draft flags an uncertain language claim. Check the review box before approving. Nothing was approved."
        )
    conn.execute(
        """
        UPDATE drafts
        SET content_status='approved', human_reviewed=?, updated_at=?
        WHERE id=?
        """,
        (1 if human_reviewed or draft["review_required"] else 0, utc_now_iso(), draft_id),
    )
    updated = get_draft(conn, draft_id)
    if updated is None:
        raise GenerationError("Draft not found.")
    return updated


def list_slots(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM calendar_slots ORDER BY weekday, id"
    ).fetchall()
    return [dict(row) for row in rows]


def slots_for_weekday(conn: sqlite3.Connection, weekday: int, enabled_only: bool = True) -> list[dict]:
    sql = "SELECT * FROM calendar_slots WHERE weekday = ?"
    args: list[object] = [weekday]
    if enabled_only:
        sql += " AND enabled = 1"
    sql += " ORDER BY id"
    return [dict(row) for row in conn.execute(sql, args)]


def save_slot(
    conn: sqlite3.Connection,
    *,
    weekday: int,
    category: str,
    platform: str,
    count: int,
    enabled: bool,
    notes: str,
    slot_id: int | None = None,
) -> None:
    if weekday not in range(7):
        raise GenerationError("Weekday must be Monday through Sunday.")
    if category not in CATEGORY_LABELS or platform not in PLATFORM_LABELS:
        raise GenerationError("Choose a real category and platform.")
    if not 1 <= count <= 10:
        raise GenerationError("Each slot can create 1 to 10 drafts.")
    notes = notes.strip()[:300]
    if slot_id is None:
        try:
            conn.execute(
                """
                INSERT INTO calendar_slots (weekday, category, platform, count, enabled, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (weekday, category, platform, count, 1 if enabled else 0, notes),
            )
        except sqlite3.IntegrityError as exc:
            raise GenerationError("That weekday, category, and platform already exist. Nothing was added.") from exc
        return
    try:
        conn.execute(
            """
            UPDATE calendar_slots
            SET weekday=?, category=?, platform=?, count=?, enabled=?, notes=?
            WHERE id=?
            """,
            (weekday, category, platform, count, 1 if enabled else 0, notes, slot_id),
        )
    except sqlite3.IntegrityError as exc:
        raise GenerationError("That weekday, category, and platform already exist. Nothing was changed.") from exc


def delete_slot(conn: sqlite3.Connection, slot_id: int) -> None:
    conn.execute("DELETE FROM calendar_slots WHERE id = ?", (slot_id,))


def category_keys() -> list[str]:
    return [key for key, _label in CATEGORIES]
