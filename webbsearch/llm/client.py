"""Thin Claude client: timeouts, structured output via forced tool use, prompt
caching, response cache, and usage accounting. Every caller has a
deterministic fallback, so search never fails because the LLM is slow or down.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from webbsearch.config import settings

# USD per million tokens (input, output), from platform.claude.com/docs/en/about-claude/pricing
# (checked Sep 2026). Batch API is 50% of these; cache reads are 0.1x input (0.05x on Opus 5.5).
PRICES = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def error_detail(e: Exception) -> str:
    """A short, readable reason for a failed call: the HTTP status and Anthropic's message, for the dashboard."""
    import re as _re
    text = str(e)
    m = _re.search(r"'message': ['\"](.+?)['\"]\}", text)
    msg = m.group(1) if m else text
    code = getattr(e, "status_code", None)
    return (f"{code}: " if code else f"{type(e).__name__}: ")[:40] + msg[:240]


def call_cost(model: str, u) -> float:
    """Estimated USD for one call's usage: input, cache reads at 0.1x, cache writes at 1.25x, output."""
    if u is None:
        return 0.0
    it, ot = getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0
    cr = getattr(u, "cache_read_input_tokens", 0) or 0
    cw = getattr(u, "cache_creation_input_tokens", 0) or 0
    pi, po = PRICES.get(model, (3.0, 15.0))
    return (it * pi + cr * pi * 0.1 + cw * pi * 1.25 + ot * po) / 1e6


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    timeouts: int = 0
    errors: int = 0
    cache_hits: int = 0
    latency_ms: list = field(default_factory=list)

    def add(self, model: str, u, ms: float):
        self.calls += 1
        it, ot = getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0
        cr = getattr(u, "cache_read_input_tokens", 0) or 0
        cw = getattr(u, "cache_creation_input_tokens", 0) or 0
        self.input_tokens += it; self.output_tokens += ot; self.cache_read_tokens += cr
        self.cost_usd += call_cost(model, u)
        self.latency_ms.append(ms)


class LRU(OrderedDict):
    def __init__(self, n=50_000):
        super().__init__(); self.n = n

    def get2(self, k):
        if k in self:
            self.move_to_end(k); return self[k]
        return None

    def put(self, k, v):
        self[k] = v; self.move_to_end(k)
        if len(self) > self.n:
            self.popitem(last=False)


class ClaudeClient:
    def __init__(self, api_key: str | None = None, cache_dir: Path | None = None, client=None):
        self.enabled = client is not None or (bool(api_key or settings.anthropic_api_key) and not settings.llm_disabled)
        self._client = client
        if self._client is None and self.enabled:
            import anthropic
            self._client = anthropic.Anthropic(api_key=api_key or settings.anthropic_api_key, max_retries=0)
        self.usage = Usage()
        self.mem = LRU()
        self.cache_dir = cache_dir or (settings.data_dir / "llm_cache")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._disk: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._day, self._day_calls = "", 0
        self.recorder = None          # the analytics store's record_llm (server.py), for the dashboard's Claude usage
        self._load_disk()

    def _record(self, model: str, purpose: str, status: str, usage=None, ms: float = 0.0, detail: str = "") -> None:
        if self.recorder is not None:
            try:
                kw = {"detail": detail} if detail else {}
                self.recorder(model=model, purpose=purpose, status=status, usage=usage, ms=ms, **kw)
            except Exception:  # noqa: BLE001  (recording never breaks a search)
                pass

    # -------------------------------------------------------------- daily cap (public demo)
    def _spend(self) -> bool:
        """Count one API call against WEBB_CLAUDE_DAILY_CALLS (UTC day; 0 or unset = no cap). False once the day's
        calls are used up: the caller falls back as if Claude were off. Cached answers never count."""
        cap = int(os.environ.get("WEBB_CLAUDE_DAILY_CALLS", "0") or 0)
        if cap <= 0:
            return True
        with self._lock:
            day = time.strftime("%Y-%m-%d", time.gmtime())
            if day != self._day:
                self._day, self._day_calls = day, 0
            if self._day_calls >= cap:
                return False
            self._day_calls += 1
            return True

    def capped(self) -> bool:
        cap = int(os.environ.get("WEBB_CLAUDE_DAILY_CALLS", "0") or 0)
        return cap > 0 and self._day == time.strftime("%Y-%m-%d", time.gmtime()) and self._day_calls >= cap

    # -------------------------------------------------------------- caching
    def _load_disk(self):
        f = self.cache_dir / "responses.jsonl"
        if f.exists():
            for line in f.read_text().splitlines():
                try:
                    r = json.loads(line); self._disk[r["k"]] = r["v"]
                except Exception:
                    pass

    def _persist(self, k, v):
        with self._lock:
            self._disk[k] = v
            with open(self.cache_dir / "responses.jsonl", "a") as fh:
                fh.write(json.dumps({"k": k, "v": v}) + "\n")

    @staticmethod
    def key(*parts) -> str:
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]

    def cached(self, k: str):
        v = self.mem.get2(k)
        if v is None:
            v = self._disk.get(k)
            if v is not None:
                self.mem.put(k, v)
        if v is not None:
            self.usage.cache_hits += 1
        return v

    # -------------------------------------------------------------- calls
    def structured(self, *, model: str, system: str, user: str, tool_name: str, schema: dict,
                   timeout: float, max_tokens: int = 600, cache_key: str | None = None,
                   persist: bool = True) -> dict | None:
        """Forced single tool call -> validated dict. None on timeout/error/disabled."""
        if cache_key:
            hit = self.cached(cache_key)
            if hit is not None:
                return hit
        if not self.enabled:
            return None
        if not self._spend():
            self._record(model, tool_name, "capped")
            return None
        t = time.time()
        try:
            resp = self._client.messages.create(
                model=model, max_tokens=max_tokens, timeout=timeout,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                tools=[{"name": tool_name, "description": f"Return the {tool_name} result.", "input_schema": schema}],
                tool_choice={"type": "tool", "name": tool_name},
            )
        except Exception as e:  # timeouts, rate limits, network: degrade silently
            timeout = "timeout" in type(e).__name__.lower() or "Timeout" in str(e)
            if timeout:
                self.usage.timeouts += 1
            else:
                self.usage.errors += 1
            self._record(model, tool_name, "timeout" if timeout else "error", ms=(time.time() - t) * 1000,
                         detail=error_detail(e))
            return None
        self.usage.add(model, resp.usage, (time.time() - t) * 1000)
        self._record(model, tool_name, "ok", resp.usage, (time.time() - t) * 1000)
        if getattr(resp, "stop_reason", None) == "max_tokens":  # truncated tool input: don't trust it
            self.usage.errors += 1
            return None
        out = next((b.input for b in resp.content if getattr(b, "type", "") == "tool_use"), None)
        if out is not None and cache_key:
            self.mem.put(cache_key, out)
            if persist:
                self._persist(cache_key, out)
        return out

    def messages(self, **kw):
        """Raw Messages API (agent loop). Caller handles fallbacks."""
        if not self.enabled:
            raise RuntimeError("FlexIQ is off")
        purpose = (kw.get("tool_choice") or {}).get("name") or ("agent" if kw.get("tools") else "message")
        if not self._spend():
            self._record(kw.get("model", ""), purpose, "capped")
            raise RuntimeError("FlexIQ's daily limit for this demo is used up")
        t = time.time()
        try:
            resp = self._client.messages.create(**kw)
        except Exception as e:
            self._record(kw.get("model", ""), purpose,
                         "timeout" if "timeout" in type(e).__name__.lower() else "error", ms=(time.time() - t) * 1000,
                         detail=error_detail(e))
            raise
        self.usage.add(kw["model"], resp.usage, (time.time() - t) * 1000)
        self._record(kw["model"], purpose, "ok", resp.usage, (time.time() - t) * 1000)
        return resp

    def stats(self) -> dict:
        lat = sorted(self.usage.latency_ms)
        p = lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))], 1) if lat else None
        u = self.usage
        return {"enabled": self.enabled, "capped": self.capped(), "calls": u.calls, "cache_hits": u.cache_hits, "timeouts": u.timeouts,
                "errors": u.errors, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                "cost_usd": round(u.cost_usd, 4), "p50_ms": p(.5), "p95_ms": p(.95)}
