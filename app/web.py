import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.analytics.service import NOT_AVAILABLE, counts, engagement_view, refresh_engagement, suggest_topics
from app.backup import BackupError, create_backup, list_backups, resolve_backup_name, restore_into
from app.config import Settings, bind_host
from app.content.ollama import OllamaClient, OllamaUnavailable, check_ollama
from app.content.schema import CATEGORIES, CATEGORY_LABELS, PLATFORM_LABELS, PLATFORMS, WEEKDAYS
from app.content.service import (
    GenerationError,
    approve_draft,
    delete_slot,
    effective_ollama,
    generate_batch,
    get_draft,
    list_drafts,
    list_slots,
    regenerate_draft,
    save_edit,
    save_slot,
)
from app.db import connect, get_setting, set_setting
from app.leads.service import PREFERENCES, TRIALS, LeadError, get_lead, list_leads, save_lead
from app.logging_setup import log_event
from app.publishing.adapters import get_adapter
from app.publishing.service import (
    PublishRefused,
    acknowledge_uncertain,
    attempt_publish,
    export_text,
    get_item,
    list_items,
    move_item,
    queue_draft,
)
from app.research.service import ResearchError, import_manual, list_opportunities, set_approval
from app.scheduler import apply_scheduler

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
NAV = [
    ("/", "Overview"),
    ("/calendar", "Content calendar"),
    ("/drafts", "Drafts"),
    ("/publishing", "Publishing"),
    ("/opportunities", "Community"),
    ("/leads", "Leads"),
    ("/analytics", "Analytics"),
    ("/settings", "Settings"),
    ("/logs", "Logs and backups"),
]
_MODEL = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def nav_active(path: str, href: str) -> bool:
    if href == "/":
        return path == "/"
    return path == href or path.startswith(href + "/")


TEMPLATES.env.globals["nav_items"] = NAV
TEMPLATES.env.globals["categories"] = CATEGORIES
TEMPLATES.env.globals["platforms"] = PLATFORMS
TEMPLATES.env.globals["weekdays"] = WEEKDAYS
TEMPLATES.env.globals["nav_active"] = nav_active
TEMPLATES.env.globals["preferences"] = PREFERENCES
TEMPLATES.env.globals["trials"] = TRIALS


def render(request: Request, name: str, *, status_code: int = 200, **context):
    context.setdefault("error", request.query_params.get("error", ""))
    context.setdefault("notice", request.query_params.get("notice", ""))
    context.setdefault("status_code", status_code)
    return TEMPLATES.TemplateResponse(request, name, context, status_code=status_code)


def redirect(path: str, *, notice: str = "", error: str = "") -> RedirectResponse:
    query = {}
    if notice:
        query["notice"] = notice[:300]
    if error:
        query["error"] = error[:300]
    target = path if not query else f"{path}?{urlencode(query)}"
    return RedirectResponse(target, status_code=303)


@contextmanager
def session(settings: Settings):
    conn = connect(settings.database_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _checked(form, name: str) -> bool:
    return form.get(name) in {"1", "on", "true", "yes"}


def _draft_payload(form) -> dict:
    carousel = str(form.get("carousel_outline") or "")
    return {
        "topic_key": form.get("topic_key") or "",
        "category": form.get("category") or "",
        "platform": form.get("platform") or "",
        "hook": form.get("hook") or "",
        "english_examples": str(form.get("english_examples") or ""),
        "korean_explanation": form.get("korean_explanation") or "",
        "engagement_question": form.get("engagement_question") or "",
        "cta": form.get("cta") or "",
        "hashtags": str(form.get("hashtags") or ""),
        "carousel_outline": carousel or None,
        "uncertain_linguistic_claim": _checked(form, "uncertain_linguistic_claim"),
        "uncertainty_reason": form.get("uncertainty_reason") or "",
    }


def _validate_ollama_host(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("Ollama host must be on this computer (127.0.0.1 or localhost).")
    return value.strip().rstrip("/")


def _validate_model(value: str) -> str:
    if not _MODEL.fullmatch(value.strip()):
        raise ValueError("Model name may contain letters, numbers, and . _ : - only.")
    return value.strip()


def _credential_flags(settings: Settings) -> dict:
    return {
        "facebook": bool(settings.facebook_page_id and settings.facebook_page_token),
        "instagram": bool(settings.instagram_user_id and settings.instagram_access_token),
        "instagram_image": (settings.instagram_image_url or "").startswith("https://"),
        "naver_cafe": bool(
            settings.naver_cafe_access_token and settings.naver_cafe_club_id and settings.naver_cafe_menu_id
        ),
    }


def _can_publish(item: dict, settings: Settings) -> bool:
    adapter = get_adapter(item["platform"])
    if not adapter.automated or not adapter.credentials_ready(settings):
        return False
    if item["platform"] == "instagram" and not adapter.image_ready(settings):
        return False
    if item["retry_blocked"] or item["status"] in {"publishing", "published", "manual-action-required", "draft"}:
        return False
    return item["status"] in {"approved", "scheduled", "failed"}


def register_routes(app: FastAPI) -> None:
    @app.exception_handler(404)
    async def not_found(request: Request, _exc: Exception):
        return render(request, "error.html", status_code=404, message="That page does not exist.")

    @app.exception_handler(500)
    async def crashed(request: Request, _exc: Exception):
        return render(
            request,
            "error.html",
            status_code=500,
            message="Something went wrong. The details are in the log. Nothing was published.",
        )

    @app.get("/health")
    def health(request: Request):
        settings = request.app.state.settings
        with session(settings) as conn:
            jobs = {
                "weekly_calendar": get_setting(conn, "job_weekly_calendar") == "1",
                "research_rss": get_setting(conn, "job_research_rss") == "1",
            }
        return {"ok": True, "bind": bind_host(), "jobs": jobs}

    @app.get("/")
    def overview(request: Request):
        settings = request.app.state.settings
        with session(settings) as conn:
            host, model = effective_ollama(conn, settings)
            return render(
                request,
                "overview.html",
                stats=counts(conn),
                jobs={
                    "weekly_calendar": get_setting(conn, "job_weekly_calendar") == "1",
                    "research_rss": get_setting(conn, "job_research_rss") == "1",
                    "calendar_status": get_setting(conn, "job_weekly_calendar_last_status") or "never run",
                    "research_status": get_setting(conn, "job_research_rss_last_status") or "never run",
                },
                ollama_host=host,
                ollama_model=model,
            )

    @app.get("/calendar")
    def calendar_page(request: Request):
        with session(request.app.state.settings) as conn:
            return render(request, "calendar.html", slots=list_slots(conn))

    @app.post("/calendar")
    async def calendar_save(request: Request):
        form = await request.form()
        try:
            weekday = int(str(form.get("weekday")))
            count = int(str(form.get("count") or "1"))
        except ValueError:
            return redirect("/calendar", error="Weekday and count must be numbers. Nothing was saved.")
        slot_raw = str(form.get("slot_id") or "").strip()
        slot_id = int(slot_raw) if slot_raw.isdigit() else None
        with session(request.app.state.settings) as conn:
            try:
                save_slot(
                    conn,
                    weekday=weekday,
                    category=str(form.get("category") or ""),
                    platform=str(form.get("platform") or ""),
                    count=count,
                    enabled=_checked(form, "enabled"),
                    notes=str(form.get("notes") or ""),
                    slot_id=slot_id,
                )
                log_event(conn, "info", "Calendar slot saved. The network was not called.")
            except GenerationError as exc:
                return redirect("/calendar", error=str(exc))
        return redirect("/calendar", notice="Calendar saved. Nothing was generated.")

    @app.post("/calendar/{slot_id}/delete")
    def calendar_delete(request: Request, slot_id: int):
        with session(request.app.state.settings) as conn:
            delete_slot(conn, slot_id)
            log_event(conn, "info", f"Calendar slot {slot_id} deleted.")
        return redirect("/calendar", notice="Slot deleted.")

    @app.get("/drafts")
    def drafts_page(request: Request):
        category = request.query_params.get("category") or ""
        status = request.query_params.get("status") or ""
        if category and category not in CATEGORY_LABELS:
            category = ""
        if status and status not in {"draft", "approved"}:
            status = ""
        with session(request.app.state.settings) as conn:
            return render(
                request,
                "drafts.html",
                drafts=list_drafts(conn, category or None, status or None),
                selected_category=category,
                selected_status=status,
            )

    @app.post("/drafts")
    async def drafts_generate(request: Request):
        form = await request.form()
        try:
            count = int(str(form.get("count") or "0"))
        except ValueError:
            count = 0
        categories = [str(item) for item in form.getlist("categories")]
        platform = str(form.get("platform") or "")
        errors = []
        if not 1 <= count <= 10:
            errors.append("Count must be between 1 and 10.")
        if not categories or any(item not in CATEGORY_LABELS for item in categories):
            errors.append("Choose at least one real category.")
        if platform not in PLATFORM_LABELS:
            errors.append("Choose a platform.")
        settings = request.app.state.settings
        if errors:
            with session(settings) as conn:
                return render(
                    request,
                    "drafts.html",
                    status_code=400,
                    error=" ".join(errors) + " Nothing was generated.",
                    drafts=list_drafts(conn),
                    selected_category="",
                    selected_status="",
                )
        with session(settings) as conn:
            host, model = effective_ollama(conn, settings)
            client = OllamaClient(host, model, settings.ollama_timeout)
            try:
                created, note = generate_batch(
                    conn,
                    client,
                    count=count,
                    categories=categories,
                    platform=platform,
                    attempts=settings.generation_attempts,
                )
            except OllamaUnavailable as exc:
                conn.rollback()
                log_event(conn, "error", str(exc))
                return redirect("/drafts", error=str(exc))
            log_event(conn, "info", f"Generated {len(created)} draft(s).")
        if note and not created:
            return redirect("/drafts", error=note)
        if note:
            return redirect("/drafts", notice=f"Created {len(created)} draft(s). Then: {note}"[:300])
        return redirect("/drafts", notice=f"Created {len(created)} draft(s).")

    @app.get("/drafts/{draft_id}")
    def draft_detail(request: Request, draft_id: int):
        with session(request.app.state.settings) as conn:
            draft = get_draft(conn, draft_id)
            if draft is None:
                return render(request, "error.html", status_code=404, message="That draft does not exist.")
            return render(request, "draft_detail.html", draft=draft)

    @app.post("/drafts/{draft_id}/save")
    async def draft_save(request: Request, draft_id: int):
        form = await request.form()
        with session(request.app.state.settings) as conn:
            current = get_draft(conn, draft_id)
            if current is None:
                return render(request, "error.html", status_code=404, message="That draft does not exist.")
            try:
                save_edit(conn, draft_id, _draft_payload(form))
            except GenerationError as exc:
                draft = get_draft(conn, draft_id) or current
                return render(request, "draft_detail.html", status_code=400, error=str(exc), draft=draft)
            log_event(conn, "info", f"Draft {draft_id} saved and returned to draft status.")
        return redirect(f"/drafts/{draft_id}", notice="Saved. Status is draft again until you approve it.")

    @app.post("/drafts/{draft_id}/regenerate")
    def draft_regenerate(request: Request, draft_id: int):
        settings = request.app.state.settings
        with session(settings) as conn:
            host, model = effective_ollama(conn, settings)
            client = OllamaClient(host, model, settings.ollama_timeout)
            try:
                regenerate_draft(conn, client, draft_id, settings.generation_attempts)
            except OllamaUnavailable as exc:
                log_event(conn, "error", str(exc))
                return redirect(f"/drafts/{draft_id}", error=str(exc))
            except GenerationError as exc:
                return redirect(f"/drafts/{draft_id}", error=str(exc))
            log_event(conn, "info", f"Draft {draft_id} regenerated.")
        return redirect(f"/drafts/{draft_id}", notice="Regenerated. Review the new text before approving.")

    @app.post("/drafts/{draft_id}/approve")
    async def draft_approve(request: Request, draft_id: int):
        form = await request.form()
        with session(request.app.state.settings) as conn:
            try:
                approve_draft(conn, draft_id, _checked(form, "human_reviewed"))
            except GenerationError as exc:
                return redirect(f"/drafts/{draft_id}", error=str(exc))
            log_event(conn, "info", f"Draft {draft_id} approved.")
        return redirect(f"/drafts/{draft_id}", notice="Approved. Nothing was published.")

    @app.post("/drafts/{draft_id}/queue")
    def draft_queue(request: Request, draft_id: int):
        with session(request.app.state.settings) as conn:
            try:
                item = queue_draft(conn, draft_id)
            except PublishRefused as exc:
                return redirect(f"/drafts/{draft_id}", error=str(exc))
            log_event(conn, "info", f"Draft {draft_id} queued as {item['status']}.")
        return redirect(f"/publishing/{item['id']}", notice="Added to the publishing queue. Nothing was posted.")

    @app.get("/publishing")
    def publishing_page(request: Request):
        with session(request.app.state.settings) as conn:
            return render(request, "publishing.html", items=list_items(conn))

    @app.get("/publishing/{item_id}")
    def publishing_detail(request: Request, item_id: int):
        settings = request.app.state.settings
        with session(settings) as conn:
            item = get_item(conn, item_id)
            if item is None:
                return render(request, "error.html", status_code=404, message="That publish item does not exist.")
            adapter = get_adapter(item["platform"])
            return render(
                request,
                "publishing_detail.html",
                item=item,
                summary=adapter.summary,
                automated=adapter.automated,
                can_publish=_can_publish(item, settings),
                credentials=_credential_flags(settings),
            )

    @app.post("/publishing/{item_id}/publish")
    def publishing_publish(request: Request, item_id: int):
        settings = request.app.state.settings
        with session(settings) as conn:
            result = attempt_publish(conn, item_id, settings, request.app.state.transport)
            log_event(conn, "info" if result["published"] else "warning", result["detail"])
        if result["published"]:
            return redirect(f"/publishing/{item_id}", notice=result["detail"])
        return redirect(f"/publishing/{item_id}", error=result["detail"])

    @app.post("/publishing/{item_id}/manual")
    def publishing_manual(request: Request, item_id: int):
        with session(request.app.state.settings) as conn:
            try:
                move_item(conn, item_id, "manual-action-required")
            except PublishRefused as exc:
                return redirect(f"/publishing/{item_id}", error=str(exc))
            log_event(conn, "info", f"Publish item {item_id} moved to manual export. Nothing was posted.")
        return redirect(f"/publishing/{item_id}", notice="Moved to the manual queue. Nothing was posted.")

    @app.post("/publishing/{item_id}/schedule")
    async def publishing_schedule(request: Request, item_id: int):
        form = await request.form()
        raw = str(form.get("scheduled_for") or "")
        try:
            when = datetime.strptime(raw, "%Y-%m-%dT%H:%M").isoformat()
        except ValueError:
            return redirect(f"/publishing/{item_id}", error="Enter a date and time. Nothing was scheduled.")
        with session(request.app.state.settings) as conn:
            try:
                move_item(conn, item_id, "scheduled", scheduled_for=when)
            except PublishRefused as exc:
                return redirect(f"/publishing/{item_id}", error=str(exc))
            log_event(conn, "info", f"Publish item {item_id} scheduled locally for {when}. Nothing was posted.")
        return redirect(
            f"/publishing/{item_id}",
            notice="Saved on this computer only. The scheduler will not post it.",
        )

    @app.post("/publishing/{item_id}/unschedule")
    def publishing_unschedule(request: Request, item_id: int):
        with session(request.app.state.settings) as conn:
            try:
                move_item(conn, item_id, "approved", scheduled_for=None)
            except PublishRefused as exc:
                return redirect(f"/publishing/{item_id}", error=str(exc))
        return redirect(f"/publishing/{item_id}", notice="Schedule cleared. Nothing was posted.")

    @app.post("/publishing/{item_id}/acknowledge")
    def publishing_ack(request: Request, item_id: int):
        with session(request.app.state.settings) as conn:
            try:
                acknowledge_uncertain(conn, item_id)
            except PublishRefused as exc:
                return redirect(f"/publishing/{item_id}", error=str(exc))
            log_event(conn, "warning", f"Publish item {item_id} retry unlocked by the user. Nothing was published.")
        return redirect(
            f"/publishing/{item_id}",
            notice="Retry unlocked. A second request can create a duplicate if the first one actually landed. Nothing was published.",
        )

    @app.get("/publishing/{item_id}/export")
    def publishing_export(request: Request, item_id: int):
        with session(request.app.state.settings) as conn:
            try:
                text = export_text(conn, item_id)
            except PublishRefused as exc:
                return PlainTextResponse(str(exc), status_code=404)
        return PlainTextResponse(
            text,
            headers={"Content-Disposition": f'attachment; filename="post-{item_id}.txt"'},
        )

    @app.get("/opportunities")
    def opportunities_page(request: Request):
        with session(request.app.state.settings) as conn:
            return render(request, "opportunities.html", opportunities=list_opportunities(conn))

    @app.post("/opportunities/import")
    async def opportunities_import(request: Request):
        form = await request.form()
        with session(request.app.state.settings) as conn:
            try:
                import_manual(
                    conn,
                    url=str(form.get("url") or ""),
                    topic=str(form.get("topic") or ""),
                    excerpt=str(form.get("excerpt") or ""),
                    reply_language=str(form.get("reply_language") or "ko"),
                )
                log_event(conn, "info", "Manual opportunity imported. Nothing was sent.")
            except ResearchError as exc:
                return redirect("/opportunities", error=str(exc))
        return redirect("/opportunities", notice="Imported. Nothing was sent.")

    @app.post("/opportunities/{opportunity_id}/approve")
    def opportunities_approve(request: Request, opportunity_id: int):
        with session(request.app.state.settings) as conn:
            try:
                set_approval(conn, opportunity_id, "approved")
            except ResearchError as exc:
                return redirect("/opportunities", error=str(exc))
            log_event(conn, "info", f"Opportunity {opportunity_id} approved. Nothing was submitted.")
        return redirect("/opportunities", notice="Marked approved. This app has no send action, and nothing was submitted.")

    @app.post("/opportunities/{opportunity_id}/reject")
    def opportunities_reject(request: Request, opportunity_id: int):
        with session(request.app.state.settings) as conn:
            try:
                set_approval(conn, opportunity_id, "rejected")
            except ResearchError as exc:
                return redirect("/opportunities", error=str(exc))
        return redirect("/opportunities", notice="Marked rejected. Nothing was submitted.")

    @app.get("/leads")
    def leads_page(request: Request):
        with session(request.app.state.settings) as conn:
            return render(request, "leads.html", leads=list_leads(conn), editing=None)

    @app.get("/leads/{lead_id}")
    def lead_edit(request: Request, lead_id: int):
        with session(request.app.state.settings) as conn:
            lead = get_lead(conn, lead_id)
            if lead is None:
                return render(request, "error.html", status_code=404, message="That lead does not exist.")
            return render(request, "leads.html", leads=list_leads(conn), editing=lead)

    @app.post("/leads")
    async def leads_create(request: Request):
        return await _save_lead_request(request, None)

    @app.post("/leads/{lead_id}")
    async def leads_update(request: Request, lead_id: int):
        return await _save_lead_request(request, lead_id)

    async def _save_lead_request(request: Request, lead_id: int | None):
        form = await request.form()
        follow = str(form.get("follow_up_on") or "").strip() or None
        payload = {
            "display_name": form.get("display_name") or "",
            "source": form.get("source") or "",
            "inquiry_date": form.get("inquiry_date") or "",
            "learning_goal": form.get("learning_goal") or "",
            "consent": _checked(form, "consent"),
            "communication_preference": form.get("communication_preference") or "",
            "trial_lesson_status": form.get("trial_lesson_status") or "not_offered",
            "follow_up_on": follow,
            "notes": form.get("notes") or "",
            "allow_duplicate": _checked(form, "allow_duplicate"),
        }
        with session(request.app.state.settings) as conn:
            try:
                saved = save_lead(conn, payload, lead_id)
            except LeadError as exc:
                return redirect("/leads" if lead_id is None else f"/leads/{lead_id}", error=str(exc))
            log_event(conn, "info", f"Lead {saved['id']} saved. No message was sent.")
        if saved.get("ignored_follow_up"):
            return redirect(
                f"/leads/{saved['id']}",
                notice="Lead saved. The follow-up was not marked due because consent is off.",
            )
        return redirect(f"/leads/{saved['id']}", notice="Lead saved. No message was sent.")

    @app.get("/analytics")
    def analytics_page(request: Request):
        with session(request.app.state.settings) as conn:
            engagement = engagement_view(conn)
            return render(
                request,
                "analytics.html",
                stats=counts(conn),
                suggestions=suggest_topics(conn),
                engagement=engagement,
                not_available=NOT_AVAILABLE,
            )

    @app.post("/analytics/engagement")
    def analytics_refresh(request: Request):
        settings = request.app.state.settings
        with session(settings) as conn:
            result = refresh_engagement(conn, settings, request.app.state.transport)
            log_event(conn, "info", result["detail"])
        if result["status"] == NOT_AVAILABLE:
            return redirect("/analytics", error=result["detail"])
        return redirect("/analytics", notice=result["detail"])

    @app.get("/settings")
    def settings_page(request: Request):
        settings = request.app.state.settings
        with session(settings) as conn:
            host, model = effective_ollama(conn, settings)
            return render(
                request,
                "settings.html",
                ollama_host=host,
                ollama_model=model,
                calendar_on=get_setting(conn, "job_weekly_calendar") == "1",
                research_on=get_setting(conn, "job_research_rss") == "1",
                calendar_status=get_setting(conn, "job_weekly_calendar_last_status") or "never run",
                research_status=get_setting(conn, "job_research_rss_last_status") or "never run",
                credentials=_credential_flags(settings),
                port=settings.app_port,
            )

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        settings = request.app.state.settings
        host_raw = str(form.get("ollama_host") or "").strip()
        model_raw = str(form.get("ollama_model") or "").strip()
        try:
            host = _validate_ollama_host(host_raw) if host_raw else ""
            model = _validate_model(model_raw) if model_raw else ""
        except ValueError as exc:
            return redirect("/settings", error=str(exc))
        calendar_on = _checked(form, "job_weekly_calendar")
        research_on = _checked(form, "job_research_rss")
        with session(settings) as conn:
            set_setting(conn, "ollama_host", host)
            set_setting(conn, "ollama_model", model)
            set_setting(conn, "job_weekly_calendar", "1" if calendar_on else "0")
            set_setting(conn, "job_research_rss", "1" if research_on else "0")
            apply_scheduler(request.app.state.scheduler, calendar_on, research_on)
            log_event(conn, "info", "Settings saved. Jobs still do not publish.")
        return redirect("/settings", notice="Settings saved. Jobs still do not publish.")

    @app.post("/settings/ollama-check")
    def settings_ollama(request: Request):
        with session(request.app.state.settings) as conn:
            host, _model = effective_ollama(conn, request.app.state.settings)
        result = check_ollama(host)
        if result["ok"]:
            names = ", ".join(result["models"]) or "no models listed"
            return redirect("/settings", notice=f"Ollama is reachable. Models: {names}")
        return redirect("/settings", error=result["detail"])

    @app.get("/logs")
    def logs_page(request: Request):
        settings = request.app.state.settings
        with session(settings) as conn:
            rows = conn.execute("SELECT * FROM app_logs ORDER BY id DESC LIMIT 200").fetchall()
            return render(
                request,
                "logs.html",
                logs=[dict(row) for row in rows],
                backups=list_backups(settings.backup_dir),
            )

    @app.post("/backups")
    def backups_create(request: Request):
        settings = request.app.state.settings
        try:
            path = create_backup(settings.database_path, settings.backup_dir)
        except BackupError as exc:
            return redirect("/logs", error=str(exc))
        with session(settings) as conn:
            log_event(conn, "info", f"Backup created: {path.name}")
        return redirect("/logs", notice=f"Backup saved: {path.name}")

    @app.post("/backups/restore")
    async def backups_restore(request: Request):
        form = await request.form()
        if not _checked(form, "confirm_restore"):
            return redirect("/logs", error="Nothing was restored. Confirm the restore checkbox first.")
        settings = request.app.state.settings
        try:
            path = resolve_backup_name(settings.backup_dir, str(form.get("name") or ""))
        except BackupError as exc:
            return redirect("/logs", error=str(exc))
        with session(settings) as conn:
            try:
                restore_into(conn, str(path))
            except BackupError as exc:
                return redirect("/logs", error=str(exc))
            log_event(conn, "warning", f"Database restored from {path.name}")
        return redirect("/logs", notice=f"Restored {path.name}.")

    @app.get("/backups/{name}")
    def backups_download(request: Request, name: str):
        try:
            path = resolve_backup_name(request.app.state.settings.backup_dir, name)
        except BackupError as exc:
            return PlainTextResponse(str(exc), status_code=404)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")
