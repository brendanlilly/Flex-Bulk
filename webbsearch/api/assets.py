"""Pages always load the scripts and styles they were written with.

After an update a browser could keep the previous app.css or app.js from its cache and pair it with the new page
(v1.6's search box, with the v1.5.1 stylesheet, looked broken). Two guards:
  * each /static/*.css and *.js link in a page gets ?v=<file mtime>, so a changed file is a new URL;
  * every /static response says Cache-Control: no-cache, so the browser revalidates (a cheap 304) before reuse.
And every version label on a page (the header's #ver, the footer's .appver) is the running version, from VERSION,
whatever the HTML file says: a release never ships with last release's number again.
"""
from __future__ import annotations

import re
from pathlib import Path

_LINK = re.compile(r'((?:src|href)="/static/)([\w./-]+\.(?:css|js))(")')
_VER = re.compile(r'(<span (?:id="ver"|class="appver")>)v?[^<]*(</span>)')


def versioned(html: str, static: Path) -> str:
    def repl(m):
        f = static / m.group(2)
        v = int(f.stat().st_mtime) if f.exists() else 0
        return f"{m.group(1)}{m.group(2)}?v={v}{m.group(3)}"
    from webbsearch import __version__
    html = _VER.sub(lambda m: f"{m.group(1)}v{__version__}{m.group(2)}", html)
    return _LINK.sub(repl, html)


async def no_cache_static(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/static/") and "cache-control" not in resp.headers:
        resp.headers["Cache-Control"] = "no-cache"
    return resp
