"""Server-rendered Czech pages (accessibility.md). Templates in `templates/`, CSS in `static/`."""

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from hlidac.security.csrf import CSRF_FIELD, csrf_token

UI_DIR = Path(__file__).parent
STATIC_DIR = UI_DIR / "static"

templates = Jinja2Templates(directory=UI_DIR / "templates")  # autoescapes HTML


def render(request: Request, name: str, status_code: int = 200, **context: object) -> HTMLResponse:
    context = {"csrf_field": CSRF_FIELD, "csrf_token": csrf_token(request), **context}
    return templates.TemplateResponse(request, name, context, status_code=status_code)
