from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import Settings, bind_host, get_settings
from app.db import connect, init_db
from app.logging_setup import log_event, setup_logging
from app.scheduler import start_scheduler
from app.transport import HttpxTransport
from app.web import register_routes

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Path(settings.log_dir).mkdir(parents=True, exist_ok=True)
        Path(settings.backup_dir).mkdir(parents=True, exist_ok=True)
        Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
        setup_logging(settings.log_dir)
        conn = connect(settings.database_path)
        try:
            init_db(conn)
            log_event(conn, "info", "Studio started. Jobs that are off do not call the network.")
            conn.commit()
        finally:
            conn.close()
        app.state.scheduler = start_scheduler(settings) if settings.start_scheduler else None
        yield
        scheduler = getattr(app.state, "scheduler", None)
        if scheduler is not None:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="Local English studio", lifespan=lifespan)
    app.state.settings = settings
    app.state.transport = HttpxTransport()
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    register_routes(app)
    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run("app.main:app", host=bind_host(), port=settings.app_port, reload=False)


if __name__ == "__main__":
    main()
