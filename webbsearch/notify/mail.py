"""Outgoing e-mail (v2.3.2): quote requests to Webb's quote desk.

Every message is written to the outbox folder as an .eml file with a status file beside it, so the dashboard can show it
and a person can open it, whether or not a mail server is set. With WEBB_SMTP_HOST set it is also sent, in the
background, over SMTP with STARTTLS (port 587; WEBB_SMTP_PORT=465 uses SSL instead). Without one it stays "queued".

Settings: WEBB_SMTP_HOST, WEBB_SMTP_PORT, WEBB_SMTP_USER, WEBB_SMTP_PASSWORD, WEBB_MAIL_FROM, WEBB_MAIL_DIR."""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

_LOCK = threading.Lock()
_THREADS: list[threading.Thread] = []
_KEY = re.compile(r"^[A-Za-z0-9_-]{4,40}$")
EMAIL = re.compile(r"^[^@\s<>\"',;:()\[\]\\]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


def outbox() -> Path:
    from webbsearch.config import settings
    d = Path(os.environ.get("WEBB_MAIL_DIR") or settings.data_dir / "outbox" / "mail")
    d.mkdir(parents=True, exist_ok=True)
    return d


def header(s, n: int = 200) -> str:
    """One line, no control characters: what goes into a mail header never carries a second header."""
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(s or "")).strip()[:n]


def sender() -> str:
    f = os.environ.get("WEBB_MAIL_FROM") or ""
    if f:
        return header(f)
    u = os.environ.get("WEBB_SMTP_USER") or ""
    return u if EMAIL.match(u) else "FlexSearch quote desk <no-reply@webb-search-demo.fly.dev>"


def compose(*, to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None) -> EmailMessage:
    m = EmailMessage()
    m["From"] = sender()
    m["To"] = header(to)
    if reply_to and EMAIL.match(reply_to):
        m["Reply-To"] = reply_to
    m["Subject"] = header(subject, 240)
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain="webb-search-demo.fly.dev")
    m.set_content(text)
    if html:
        m.add_alternative(html, subtype="html")
    return m


def _write_status(key: str, st: dict) -> None:
    """Written to a temporary file and renamed, so a reader never sees half of it."""
    with _LOCK:
        f = outbox() / f"{key}.json"
        tmp = f.with_name(f"{key}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, f)


def status(key: str) -> dict | None:
    if not _KEY.match(key or ""):
        return None
    f = outbox() / f"{key}.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def eml(key: str) -> bytes | None:
    if not _KEY.match(key or ""):
        return None
    f = outbox() / f"{key}.eml"
    return f.read_bytes() if f.exists() else None


def _send(key: str, msg: EmailMessage, st: dict) -> None:
    host = os.environ.get("WEBB_SMTP_HOST", "")
    port = int(os.environ.get("WEBB_SMTP_PORT") or 587)
    user, pw = os.environ.get("WEBB_SMTP_USER"), os.environ.get("WEBB_SMTP_PASSWORD")
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=20, context=ssl.create_default_context()) as s:
                if user:
                    s.login(user, pw or "")
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                if user:
                    s.login(user, pw or "")
                s.send_message(msg)
        st |= {"status": "sent", "sent": time.time(), "error": None}
    except Exception as ex:  # noqa: BLE001  (a mail failure never fails the request; the dashboard shows it)
        st |= {"status": "failed", "error": f"{type(ex).__name__}: {str(ex)[:200]}"}
    _write_status(key, st)


def queue(key: str, msg: EmailMessage) -> dict:
    """Write the message to the outbox and send it when a mail server is set. Returns its status."""
    if not _KEY.match(key or ""):
        raise ValueError("bad mail key")
    (outbox() / f"{key}.eml").write_bytes(bytes(msg))
    st = {"key": key, "to": str(msg["To"]), "subject": str(msg["Subject"]), "created": time.time(), "sent": None,
          "error": None}
    if os.environ.get("WEBB_SMTP_HOST"):
        st["status"] = "sending"
        _write_status(key, st)
        t = threading.Thread(target=_send, args=(key, msg, dict(st)), daemon=True, name=f"mail-{key}")
        with _LOCK:
            _THREADS[:] = [x for x in _THREADS if x.is_alive()] + [t]
        t.start()
    else:
        st |= {"status": "queued", "error": "No mail server is set (WEBB_SMTP_HOST), so it waits in the outbox."}
        _write_status(key, st)
    return st


def drain(timeout: float = 10.0) -> None:
    """Wait for messages being sent (tests, shutdown)."""
    end = time.time() + timeout
    for t in list(_THREADS):
        t.join(max(0.0, end - time.time()))
