"""Start Flex Bulk on this computer: load and index the item files when they're new, then serve and open the browser.

    python -m flexbulk.launch                  # first run: demo item list if data/incoming/ is empty
    python -m flexbulk.launch --rebuild        # load and index again even if nothing changed
    python -m flexbulk.launch --port 8090 --no-browser

The files in data/incoming/ are loaded again whenever one of them is newer than the last load. The server binds to
127.0.0.1, so only this computer reaches it (set WEBB_SITE_PASSWORD and run uvicorn yourself to share it).
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("WEBB_DATA_DIR", ROOT / "data"))
INCOMING = Path(os.environ.get("FLEXBULK_INCOMING") or DATA / "incoming")
DEMO = ROOT / "samples" / "demo-data"
LOCAL = urllib.request.build_opener(urllib.request.ProxyHandler({}))     # never through a proxy to 127.0.0.1


def env() -> dict:
    e = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    e["PYTHONPATH"] = str(ROOT) + os.pathsep + e.get("PYTHONPATH", "")
    return e


def needs_build(rebuild: bool) -> str | None:
    rep = DATA / "canonical" / "load_report.json"
    if rebuild:
        return "asked to rebuild"
    if not rep.exists() or not (DATA / "index" / "products").exists():
        return "first run"
    newer = [f.name for f in INCOMING.glob("*") if f.is_file() and f.stat().st_mtime > rep.stat().st_mtime]
    return f"new file{'s' if len(newer) > 1 else ''}: {', '.join(newer[:4])}" if newer else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Start Flex Bulk on this computer")
    ap.add_argument("--port", type=int, default=8081)       # FlexSearch uses 8080; both can run
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    INCOMING.mkdir(parents=True, exist_ok=True)
    if not any(f.is_file() and not f.name.startswith(".") for f in INCOMING.iterdir()):
        print(f"No item files in {INCOMING}: using the demo item list (samples/demo-data/).")
        print("Put your items, stock and prices files there (see README) and start again to use them.")
        for f in DEMO.glob("*"):
            shutil.copy2(f, INCOMING / f.name)
    why = needs_build(a.rebuild)
    if why:
        print(f"Loading and indexing the item list ({why}); about 2 minutes for 40,000 items ...", flush=True)
        DATA.mkdir(parents=True, exist_ok=True)
        with open(DATA / "build.log", "w", encoding="utf-8") as log:
            p = subprocess.run([sys.executable, "-m", "flexbulk.build"], cwd=ROOT, env=env(), stdout=log, stderr=subprocess.STDOUT)
        if p.returncode == 0:
            import json
            r = json.loads((DATA / "canonical" / "load_report.json").read_text())
            print(f"Loaded {r['counts'].get('items', 0):,} items" + (f"; {len(r['warnings'])} note(s) from loading, shown "
                  "at the bottom of the app" if r["warnings"] else "") + ".")
        else:
            tail = (DATA / "build.log").read_text(errors="replace").splitlines()[-25:]
            print("\n".join("    " + x for x in tail))
            sys.exit(f"\nLoading failed. Full log: {DATA / 'build.log'}")
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Starting Flex Bulk at {url} (Ctrl+C stops it) ...", flush=True)
    srv = subprocess.Popen([sys.executable, "-m", "uvicorn", "flexbulk.server:app", "--host", "127.0.0.1",
                            "--port", str(a.port)], cwd=ROOT, env=env())
    try:
        for _ in range(300):
            if srv.poll() is not None:
                sys.exit("The server stopped while starting; see the messages above.")
            try:
                LOCAL.open(url + "api/health", timeout=2).read()
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        print(f"Ready: {url}  ·  settings: {url}admin", flush=True)
        if not a.no_browser:
            webbrowser.open(url)
        srv.wait()
    except KeyboardInterrupt:
        srv.terminate()


if __name__ == "__main__":
    main()
