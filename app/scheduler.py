from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import Settings
from app.content.ollama import OllamaClient, OllamaUnavailable
from app.content.service import GenerationError, effective_ollama, generate_and_save, slots_for_weekday
from app.db import connect, get_setting, init_db, set_setting
from app.logging_setup import log_event
from app.research.service import fetch_allowlisted, run_research_job
from app.clock import utc_now_iso

JOB_CALENDAR = "weekly_calendar"
JOB_RESEARCH = "research_rss"


def run_calendar_job(conn, settings: Settings, ollama: OllamaClient, now: datetime | None = None) -> dict:
    if get_setting(conn, "job_weekly_calendar") != "1":
        return {"ran": False, "reason": "disabled", "created": [], "error": ""}
    try:
        moment = now or datetime.now(ZoneInfo(settings.timezone))
    except Exception as exc:
        return {"ran": False, "reason": f"timezone: {exc}", "created": [], "error": str(exc)}
    created: list[int] = []
    error = ""
    try:
        for slot in slots_for_weekday(conn, moment.weekday(), enabled_only=True):
            for _ in range(int(slot["count"])):
                draft = generate_and_save(
                    conn,
                    ollama,
                    category=slot["category"],
                    platform=slot["platform"],
                    attempts=settings.generation_attempts,
                    calendar_slot_id=slot["id"],
                )
                created.append(int(draft["id"]))
    except OllamaUnavailable as exc:
        error = str(exc)
    except GenerationError as exc:
        error = str(exc)
    status = f"created {len(created)}" if not error else f"created {len(created)}; {error[:240]}"
    set_setting(conn, "job_weekly_calendar_last_run", utc_now_iso())
    set_setting(conn, "job_weekly_calendar_last_status", status)
    return {"ran": True, "created": created, "error": error}


def _safe_calendar(settings: Settings) -> None:
    conn = connect(settings.database_path)
    try:
        init_db(conn)
        host, model = effective_ollama(conn, settings)
        client = OllamaClient(host, model, settings.ollama_timeout)
        result = run_calendar_job(conn, settings, client)
        level = "error" if result.get("error") else "info"
        log_event(conn, level, f"Calendar job: {result}")
        conn.commit()
    except Exception as exc:
        log_event(conn, "error", f"Calendar job failed: {exc.__class__.__name__}: {exc}")
        conn.commit()
    finally:
        conn.close()


def _safe_research(settings: Settings) -> None:
    conn = connect(settings.database_path)
    try:
        init_db(conn)
        result = run_research_job(conn, fetch_allowlisted)
        level = "error" if result.get("errors") else "info"
        log_event(conn, level, f"Research job: {result}")
        conn.commit()
    except Exception as exc:
        log_event(conn, "error", f"Research job failed: {exc.__class__.__name__}: {exc}")
        conn.commit()
    finally:
        conn.close()


def apply_scheduler(scheduler: BackgroundScheduler | None, calendar_on: bool, research_on: bool) -> None:
    if scheduler is None:
        return
    _set_job(scheduler, JOB_CALENDAR, calendar_on)
    _set_job(scheduler, JOB_RESEARCH, research_on)


def _set_job(scheduler: BackgroundScheduler, job_id: str, enabled: bool) -> None:
    job = scheduler.get_job(job_id)
    if job is None:
        return
    if enabled:
        scheduler.resume_job(job_id)
    elif job.next_run_time is not None:
        scheduler.pause_job(job_id)


def start_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    conn = connect(settings.database_path)
    try:
        init_db(conn)
        calendar_on = get_setting(conn, "job_weekly_calendar") == "1"
        research_on = get_setting(conn, "job_research_rss") == "1"
    finally:
        conn.close()

    def add(func, job_id: str, enabled: bool, hour: int) -> None:
        extra = {} if enabled else {"next_run_time": None}
        scheduler.add_job(
            func,
            CronTrigger(hour=hour, minute=0, timezone=settings.timezone),
            id=job_id,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
            **extra,
        )

    add(lambda: _safe_calendar(settings), JOB_CALENDAR, calendar_on, 8)
    add(lambda: _safe_research(settings), JOB_RESEARCH, research_on, 9)
    scheduler.start()
    return scheduler
