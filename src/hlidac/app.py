"""The FastAPI app. `create_app` takes its store, clocks and email sender, so tests pass their own."""

import importlib.util
import logging
import secrets
import threading
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.middleware.sessions import SessionMiddleware

from hlidac import alerts, dev_seed, extraction, invitations
from hlidac.advice import load_cards, select_card
from hlidac.clarification import grading_attributes
from hlidac.clock import Clock, DemoClock, SystemClock
from hlidac.config import Settings, settings
from hlidac.ctcae import read_mapping
from hlidac.models import ModelGateway, VoiceGateway, build_client, origin
from hlidac.outbox_runner import SendEmail, smtp_sender, start_poller
from hlidac.pipeline import Extract, Pipeline, Transcribe
from hlidac.plans import load_plans
from hlidac.processing import make_process
from hlidac.schemas import InboundMessage, MonitoringPlan
from hlidac.security.access import LoginRequired
from hlidac.security.csrf import CsrfError, csrf_protect
from hlidac.store import InMemoryStore, Store
from hlidac.ui import STATIC_DIR, clinician, demo, login, patient, render

SESSION_COOKIE = "hlidac_session"
# Long enough that nobody is logged out during a check-in (accessibility.md: no time limits).
SESSION_MAX_AGE = timedelta(hours=12)

log = logging.getLogger(__name__)

_ERRORS = {
    403: ("Sem nemáte přístup", "Tato stránka patří jinému účtu."),
    404: ("Stránka nenalezena", "Tuto stránku jsme nenašli."),
}


def _extractor(config: Settings, store: Store, clock: Clock) -> Extract | None:
    """Free-text extraction through the gateway (WP8), or None when no model is configured: then
    written text is kept for the nurse and every question is asked with buttons."""
    try:
        gateway = ModelGateway(config, store, clock, build_client(config))
    except ValueError as error:  # not configured; the message names the missing settings only
        log.warning("free-text extraction is off: %s", error)
        return None

    def extract(message: InboundMessage, plan: MonitoringPlan, names: Sequence[str], asked=None):
        return extraction.extract(gateway, plan, message, known_names=names, asked=asked)

    return extract


def _warm_up(warm_up) -> None:
    try:
        warm_up()
    except Exception as error:  # noqa: BLE001 - the first recording then just takes longer
        log.warning("voice warm-up failed: %s", type(error).__name__)


def _voice_engine(config: Settings):
    """The transcriber VOICE_BACKEND names (D52), or None (no voice button) when it isn't set up."""
    from hlidac import transcription

    if config.voice_backend == "server":
        if not config.whisper_server_url:
            log.warning("voice is off: VOICE_BACKEND=server needs WHISPER_SERVER_URL")
            return None
        key = config.whisper_server_api_key
        return transcription.WhisperServer(
            config.whisper_server_url,
            config.whisper_server_model,
            key.get_secret_value() if key else None,
        )
    if config.voice_backend == "elevenlabs":
        if config.elevenlabs_api_key is None:
            log.warning("voice is off: VOICE_BACKEND=elevenlabs needs ELEVENLABS_API_KEY")
            return None
        log.warning("voice goes to the ElevenLabs cloud API (VOICE_BACKEND=elevenlabs, D52)")
        return transcription.ElevenLabsScribe(
            config.elevenlabs_api_key.get_secret_value(), config.elevenlabs_stt_model
        )
    if not config.whisper_model:
        return None
    if importlib.util.find_spec("mlx_whisper") is None:
        log.warning("voice is off: mlx-whisper is not installed")
        return None
    return transcription.MlxWhisper(config.whisper_model)


def _transcriber(config: Settings, store: Store, clock: Clock) -> Transcribe | None:
    """Voice (WP17): the configured transcriber behind the gateway, which lets the audio reach
    only that transcriber's own origin, or None (no voice button)."""
    whisper = _voice_engine(config)
    if whisper is None:
        return None
    allowed = frozenset() if whisper.endpoint is None else frozenset({origin(whisper.endpoint)})
    gateway = VoiceGateway(store, clock, allowed)

    def transcribe(audio: bytes, target: str) -> str:
        return gateway.transcribe(audio, whisper, target=target)

    warm_up = getattr(whisper, "warm_up", None)
    if warm_up is not None:
        transcribe.warm_up = warm_up  # type: ignore[attr-defined]  # run by the lifespan
    return transcribe


def create_app(
    config: Settings = settings,
    store: Store | None = None,
    clock: Clock | None = None,
    security_clock: Clock | None = None,
    send_email: SendEmail | None = None,
    pipeline: Pipeline | None = None,
) -> FastAPI:
    if config.secret_key is not None:
        secret = config.secret_key.get_secret_value()
    else:
        secret = secrets.token_urlsafe(32)
    iris_db = None
    if store is None and config.store_backend == "iris":
        from hlidac.iris.db import Database  # the IRIS driver only when IRIS is used
        from hlidac.iris.store import IrisStore

        # Accounts and plans come from `python -m hlidac.iris.setup` (and WP14), not dev_seed.
        iris_db = Database.from_settings(config)
        store = IrisStore(iris_db)
    elif store is None:
        store = InMemoryStore()
        clock = clock or DemoClock.from_as_of(config.as_of)
        dev_seed.seed(store, clock.now())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = None
        state = app.state
        if config.outbox_runner == "app":
            stop = start_poller(state.store, state.clock.now, state.send_email)
        invitations_stop = None
        if config.checkin_invite_minutes:  # D57: only the Azure deployment sets it
            invitations_stop = invitations.start_invitations(
                state.store,
                SystemClock().now,
                state.clock.now,
                timedelta(minutes=config.checkin_invite_minutes),
                config.checkin_invite_team,
                config.app_base_url,
            )
        warm_up = getattr(app.state.pipeline.transcribe, "warm_up", None)
        if warm_up is not None:  # load Whisper now, not on the first patient's recording
            threading.Thread(target=_warm_up, args=(warm_up,), daemon=True).start()
        yield
        if stop is not None:
            stop.set()
        if invitations_stop is not None:
            invitations_stop.set()

    app = FastAPI(
        title="Hlídač příznaků",
        lifespan=lifespan,
        dependencies=[Depends(csrf_protect)],
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = config
    app.state.store = store
    app.state.clock = clock = clock or DemoClock.from_as_of(config.as_of)
    app.state.security_clock = security_clock or SystemClock()
    app.state.secret = secret.encode()
    # Fallback contacts per team (WP18): checked whenever the clock moves or a nurse page loads.
    app.state.escalation = escalation = alerts.read_escalation()
    # Plans from data/plans/ (WP1) and the live assessment (WP4, wired in WP18): the mapping and
    # rules are checked against the plans here, so bad content fails at start-up. On IRIS (WP14,
    # D43) the treatment context comes from the hospital FHIR data; without IRIS it comes from the
    # invented demo context. Advice cards come from data/content/advice/ (WP6) and are chosen only
    # for no_escalation. Nothing is enqueued here: the Questionnaire reaches IRIS only by
    # enqueue_questionnaire.
    if pipeline is None:
        plans = load_plans()
        mapping = read_mapping()
        if iris_db is not None:
            from hlidac.iris.context import IrisContextSource

            context = IrisContextSource(iris_db, app.state.clock.now)
        else:
            context = dev_seed.context_source(app.state.clock.now())
        process = make_process(
            store,
            context,
            plans=plans.values(),
            mapping=mapping,
            select_card=select_card,
            escalation=escalation,
            base_url=config.app_base_url,
        )
        pipeline = Pipeline(
            plans=plans,
            process=process,
            advice_cards=load_cards(),
            extract=_extractor(config, store, app.state.clock),
            grading_attributes=grading_attributes(mapping),
            transcribe=_transcriber(config, store, app.state.clock),
            context=context.get_context,
        )
    app.state.pipeline = pipeline
    app.state.send_email = send_email or smtp_sender(
        config.smtp_host,
        config.smtp_port,
        config.mail_from,
        config.smtp_user,
        config.smtp_password.get_secret_value() if config.smtp_password else None,
        config.allowed_recipients(),
    )

    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie=SESSION_COOKIE,
        max_age=int(SESSION_MAX_AGE.total_seconds()),
        same_site="strict",
        https_only=config.session_https_only,
    )
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/api/health")
    def health(request: Request) -> dict:
        return {"status": "ok", "as_of": request.app.state.clock.now().date().isoformat()}

    for router in (login.public, login.signed_in, patient.router, clinician.router, demo.router):
        app.include_router(router)

    @app.exception_handler(LoginRequired)
    def to_login(request: Request, _: LoginRequired) -> RedirectResponse:
        return RedirectResponse("/prihlaseni", status_code=303)

    @app.exception_handler(CsrfError)
    def csrf_failed(request: Request, _: CsrfError) -> HTMLResponse:
        heading = "Formulář už neplatí"
        message = "Načtěte prosím stránku znovu a zkuste to ještě jednou."
        return render(request, "error.html", 403, heading=heading, message=message)

    @app.exception_handler(HTTPException)
    def http_error(request: Request, error: HTTPException) -> HTMLResponse:
        heading, message = _ERRORS.get(error.status_code, ("Chyba", "Něco se nepovedlo."))
        return render(request, "error.html", error.status_code, heading=heading, message=message)

    @app.exception_handler(Exception)
    def server_error(request: Request, _: Exception) -> HTMLResponse:
        # Czech and accessible even when something breaks; no details reach the page.
        heading, message = "Něco se pokazilo", "Zkuste to prosím za chvíli znovu."
        return render(request, "error.html", 500, heading=heading, message=message)

    return app


app = create_app()
