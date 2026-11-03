"""Factory dell'applicazione FastAPI."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.engine import Engine
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, get_settings
from .db import make_engine, make_sessionmaker
from .routes import auth, connections, deploy, jobs, pages, runs, settings as settings_routes
from .security.crypto import SecretBox
from .security.deps import NotAuthenticated
from .security.headers import HTTPSRedirectMiddleware, SecurityHeadersMiddleware
from .services.deployer import DeployService
from .services.events import EventBus
from .services.notify import notify_run
from .services.retention import after_run
from .services.scheduler import JobScheduler
from .web import render

ROOT = Path(__file__).resolve().parent.parent
ROUTERS = [
    auth.router, pages.router, connections.router, deploy.router, runs.router, jobs.router, settings_routes.router,
]


def run_migrations(engine: Engine) -> None:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "") and "hx-request" not in request.headers


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Non loggare le URL delle richieste httpx: contengono tenant ed environment ma soprattutto
    # evitano rumore; i nostri log riportano già metodo e percorso
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if settings.is_production and not settings.cookie_secure:
        raise RuntimeError("In produzione COOKIE_SECURE deve essere true (i cookie di sessione viaggiano solo in HTTPS)")
    secret_box = SecretBox(settings.fernet_keys)  # fallisce subito se le chiavi non sono valide
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(settings.db_url, settings.sqlite_journal_mode)

    factory = make_sessionmaker(engine)
    bus = EventBus()
    deploy_service = DeployService(settings, factory, secret_box, bus)
    deploy_service.on_finished.append(lambda run_id: after_run(factory, settings, run_id))
    deploy_service.on_finished.append(
        lambda run_id: notify_run(factory, settings, secret_box, run_id, transport=deploy_service.transport)
    )
    scheduler = JobScheduler(settings, engine, factory, deploy_service)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        run_migrations(engine)
        bus.bind_loop(asyncio.get_running_loop())
        n = deploy_service.recover_interrupted()
        if n:
            logging.getLogger(__name__).warning("%d esecuzioni interrotte dal riavvio precedente", n)
        scheduler.start()
        yield
        scheduler.shutdown()
        deploy_service.shutdown(wait=False)
        engine.dispose()

    app = FastAPI(title="BC Deployer", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.settings = settings
    app.state.secret_box = secret_box
    app.state.engine = engine
    app.state.sessionmaker = factory
    app.state.bus = bus
    app.state.deploy = deploy_service
    app.state.scheduler = scheduler
    app.state.bc_transport = None  # i test lo sostituiscono con un transport mockato

    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.is_production)
    if settings.is_production:
        app.add_middleware(HTTPSRedirectMiddleware)

    @app.exception_handler(NotAuthenticated)
    async def _not_authenticated(request: Request, exc: NotAuthenticated):
        if "hx-request" in request.headers:
            return Response(status_code=401, headers={"HX-Redirect": "/login?msg=session_expired"})
        if request.method == "GET" and _wants_html(request):
            return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
        return JSONResponse({"detail": "Non autenticato"}, status_code=401)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Errore"
        if _wants_html(request):
            return render(request, "error.html", exc.status_code, status=exc.status_code, detail=detail)
        return PlainTextResponse(detail, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # Non rimandare indietro i valori inviati (potrebbero contenere segreti)
        fields = ", ".join(".".join(str(p) for p in e["loc"][1:]) for e in exc.errors())
        return PlainTextResponse(f"Richiesta non valida: {fields}", status_code=422)

    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    for router in ROUTERS:
        app.include_router(router)
    return app
