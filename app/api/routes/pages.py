from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.api.auth import SESSION_KEY, consume_login_token, is_logged_in, password_ok

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).resolve().parents[3] / "frontend" / "templates"))
router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not is_logged_in(request):
        return RedirectResponse("/login", status_code=303)
    return TEMPLATES.TemplateResponse(request, "dashboard.html", {"currency": request.app.state.settings.currency})


@router.get("/app", response_class=HTMLResponse)
async def mini_app(request: Request):
    """Telegram Mini App (opened from the bot's Menu button). Data is loaded with signed initData."""
    return TEMPLATES.TemplateResponse(request, "webapp.html", {"currency": request.app.state.settings.currency})


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"error": None}
    )


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request, password: str = Form(..., max_length=200)):
    limiter = request.app.state.login_limiter
    settings = request.app.state.settings
    if limiter.blocked():
        error = "Too many failed attempts. Try again later or use a login link from the bot."
    elif password_ok(settings, password):
        request.session.clear()
        request.session[SESSION_KEY] = True
        return RedirectResponse("/", status_code=303)
    else:
        limiter.fail()
        error = "Wrong access code."
    return TEMPLATES.TemplateResponse(
        request, "login.html", {"error": error}, status_code=401
    )


@router.get("/auth/link")
async def login_link(request: Request, token: str = ""):
    if await consume_login_token(request.app.state.sf, token):
        request.session.clear()
        request.session[SESSION_KEY] = True
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login?expired=1", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/healthz")
async def health(request: Request):
    tele = getattr(request.app.state, "telethon", None)
    return {"status": "ok", "telethon_connected": bool(tele and tele.connected)}
