"""The public demo's login (v1.10.1). Everything here is off unless its environment variable is set, so a server on
a Mac behaves as before.

  WEBB_SITE_PASSWORD   a login page in front of every page and API call except /api/health (the host's health check)
                       and the login page. A signed-in visitor is trusted like the local machine: the demo account
                       switcher works for them (the accounts are synthetic; the password is what keeps strangers out).
  WEBB_ADMIN_PASSWORD  a second login for the merchandising dashboard (/admin, /api/admin/*). Without it the
                       dashboard stays local-only. The dashboard's per-start X-Admin-Token check still applies.
  WEBB_SESSION_SECRET  signs the login cookies; set it so a restart doesn't sign everyone out.

Cookies are HMACs of the secret and the password, so changing a password signs everyone out of that login."""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time
from urllib.parse import parse_qs, quote

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
OPEN = {"/api/health", "/login", "/favicon.ico"}
SITE, ADMIN = "webb_site", "webb_admin"
MAX_AGE = 30 * 86400
_RANDOM = secrets.token_hex(32)


def site_password() -> str:
    return os.environ.get("WEBB_SITE_PASSWORD", "")


def admin_password() -> str:
    return os.environ.get("WEBB_ADMIN_PASSWORD", "")


def _sign(kind: str, password: str) -> str:
    key = (os.environ.get("WEBB_SESSION_SECRET") or _RANDOM).encode()
    return hmac.new(key, f"{kind}:{password}".encode(), hashlib.sha256).hexdigest()


def _has(request: Request, cookie: str, password: str) -> bool:
    if not password:                                     # that login isn't set up (a Mac): nobody has it
        return False
    got = (getattr(request, "cookies", None) or {}).get(cookie, "")
    return bool(got) and hmac.compare_digest(got, _sign(cookie, password))


def is_local(request: Request) -> bool:
    return (request.client.host if request.client else "") in LOOPBACK


def is_open(path: str) -> bool:
    return path in OPEN


def signed_in(request: Request) -> bool:
    return _has(request, SITE, site_password())


def trusted(request: Request) -> bool:
    """The local machine, or a visitor past the site login."""
    return is_local(request) or signed_in(request)


def admin_ok(request: Request) -> bool:
    """The local machine, or a visitor past both logins."""
    return is_local(request) or (signed_in(request) and _has(request, ADMIN, admin_password()))


def client_ip(request: Request) -> str:
    """The visitor's address for the sign-in log. On Fly the connection comes from Fly's proxy, which puts the
    visitor's address in Fly-Client-IP. Logged only: access decisions never use it (see is_local)."""
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "")


def _log_signin(request: Request, kind: str, ok: bool) -> None:
    try:
        from webbsearch.api.admin import analytics
        an = analytics()
        if an is not None:
            an.record_signin(ip=client_ip(request), kind=kind, ok=ok, ua=request.headers.get("user-agent", ""))
    except Exception:  # noqa: BLE001  (the log never blocks a sign-in)
        pass


def _wants_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


def _safe_next(nxt: str) -> str:
    return nxt if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


async def middleware(request: Request, call_next):
    if not site_password() or is_open(request.url.path) or signed_in(request):
        return await call_next(request)
    if _wants_html(request):
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse("/login?next=" + quote(nxt, safe=""), 303)
    return JSONResponse({"detail": "sign in at /login"}, 401)


def login_page(request: Request, error: str = "", status: int = 200) -> HTMLResponse:
    admin = request.query_params.get("admin") == "1"
    nxt = html.escape(_safe_next(request.query_params.get("next") or ("/admin" if admin else "/")))
    title = "FlexSearch by Brendan · dashboard" if admin else "FlexSearch by Brendan"
    sub = "Merchandising dashboard: enter the admin password." if admin else \
        "A working search prototype for F.W. Webb, by Brendan. Enter the password you were given."
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    act = "/login?admin=1" if admin else "/login"
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · sign in</title>
<meta name="robots" content="noindex,nofollow">
<style>
:root{{--bg:#f6f6f7;--card:#fff;--ink:#26282c;--mute:#6f7278;--red:#c8102e;--line:#e2e3e5}}
@media (prefers-color-scheme:dark){{:root{{--bg:#16171a;--card:#202226;--ink:#ececee;--mute:#9a9ca1;--line:#33363b}}}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--ink);font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;padding:16px}}
form{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:28px;width:100%;max-width:360px}}
h1{{margin:0 0 4px;font-size:20px}}h1 b{{color:var(--red)}}p{{margin:0 0 18px;color:var(--mute);font-size:14px}}
label{{display:block;font-size:13px;margin-bottom:6px}}input{{width:100%;padding:10px 12px;font-size:15px;border:1px solid var(--line);
border-radius:6px;background:var(--bg);color:var(--ink)}}button{{margin-top:14px;width:100%;padding:10px;font-size:15px;border:0;
border-radius:6px;background:var(--red);color:#fff;cursor:pointer}}.err{{color:var(--red);margin:10px 0 0}}
</style></head><body><form method="post" action="{act}">
<h1>Flex<b>Search</b>{" · dashboard" if admin else ""}</h1><p>{sub}</p>
<label for="pw">Password</label><input id="pw" name="password" type="password" autocomplete="current-password" autofocus required>
<input type="hidden" name="next" value="{nxt}">{err}<button type="submit">Sign in</button></form></body></html>""",
                        status, headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


async def login_submit(request: Request):
    admin = request.query_params.get("admin") == "1"
    form = parse_qs((await request.body()).decode("utf-8", "replace"))
    pw = (form.get("password") or [""])[0]
    nxt = _safe_next((form.get("next") or ["/"])[0])
    want = admin_password() if admin else site_password()
    if admin and not signed_in(request) and site_password():
        return RedirectResponse("/login?next=" + quote(f"/login?admin=1&next={nxt}", safe=""), 303)
    kind = ADMIN if admin else SITE
    if not want or not hmac.compare_digest(pw.encode(), want.encode()):
        _log_signin(request, "admin" if admin else "site", False)
        time.sleep(0.5)                                  # slows guessing
        return login_page(request, "That password isn't right.", 401)
    _log_signin(request, "admin" if admin else "site", True)
    r = RedirectResponse(nxt, 303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    r.set_cookie(kind, _sign(kind, want), max_age=MAX_AGE, httponly=True, samesite="lax", secure=secure)
    return r
