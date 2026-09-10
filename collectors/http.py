"""Tiny stdlib HTTP layer with polite retries and per-source probe accounting.

Every fetch is recorded in a module-level PROBE_LOG so the pipeline can publish a
honest, machine-generated source-health report (data/latest/health.json) instead
of a hand-written "all sources OK" claim.
"""

from __future__ import annotations

import gzip
import io
import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# A descriptive UA is not optional: Reddit's public JSON paths filter generic
# agents, and nfl.com/rotowire serve different pages to bare curl.
USER_AGENT = (
    "NFLInjuryReportBot/1.0 (+https://github.com/buffedlizard55-lab/NFLInjuryReport) "
    "python-urllib/3"
)

DEFAULT_TIMEOUT = 25.0
DEFAULT_RETRIES = 2

PROBE_LOG: List[Dict[str, Any]] = []


class FetchError(RuntimeError):
    """Raised when a source cannot be read after retries."""

    def __init__(self, url: str, reason: str, status: Optional[int] = None) -> None:
        super().__init__(f"{reason} for {url}")
        self.url = url
        self.reason = reason
        self.status = status


def _open(url: str, timeout: float, headers: Dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers)
    ctx = ssl.create_default_context()
    return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def fetch_bytes(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    headers: Optional[Dict[str, str]] = None,
    source: str = "",
    referer: Optional[str] = None,
) -> bytes:
    """GET a URL, transparently decompressing gzip. Records a probe entry."""

    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Encoding": "gzip",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if referer:
        hdrs["Referer"] = referer
    if headers:
        hdrs.update(headers)

    started = time.time()
    last_reason = "unknown"
    status: Optional[int] = None
    attempt = 0

    while attempt <= retries:
        attempt += 1
        t0 = time.time()
        try:
            with _open(url, timeout, hdrs) as resp:
                raw = resp.read()
                status = resp.status
                encoding = (resp.headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip":
                    try:
                        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                    except OSError:
                        pass  # not really gzipped; keep raw
                latency_ms = int((time.time() - t0) * 1000)
                _record(source, url, status, len(raw), latency_ms, attempt, "ok", "")
                return raw
        except urllib.error.HTTPError as exc:
            status = exc.code
            last_reason = f"HTTP {exc.code} {exc.reason}"
            # 4xx will not fix itself by retrying, except 429 (rate limited).
            if 400 <= exc.code < 500 and exc.code != 429:
                break
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            last_reason = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - record and surface, never crash CI
            last_reason = f"{type(exc).__name__}: {exc}"

        if attempt <= retries:
            time.sleep(min(2.0 * attempt, 5.0))

    latency_ms = int((time.time() - started) * 1000)
    _record(source, url, status, 0, latency_ms, attempt - 1, "error", last_reason)
    raise FetchError(url, last_reason, status)


def _record(
    source: str,
    url: str,
    status: Optional[int],
    nbytes: int,
    latency_ms: int,
    attempts: int,
    result: str,
    error: str,
) -> None:
    PROBE_LOG.append(
        {
            "source": source,
            "url": url,
            "status": status,
            "bytes": nbytes,
            "latency_ms": latency_ms,
            "attempts": attempts,
            "result": result,
            "error": error,
            "probed_at": utc_now_iso(),
        }
    )


def fetch_json(url: str, **kwargs: Any) -> Any:
    raw = fetch_bytes(url, **kwargs)
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(url, f"invalid JSON: {exc}") from exc


def fetch_text(url: str, **kwargs: Any) -> str:
    raw = fetch_bytes(url, **kwargs)
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def probe(url: str, *, source: str = "", **kwargs: Any) -> Dict[str, Any]:
    """Reachability check that never raises. Used by `pipeline.py verify`."""

    before = len(PROBE_LOG)
    try:
        raw = fetch_bytes(url, retries=0, source=source, **kwargs)
        entry = dict(PROBE_LOG[-1])
        entry["reachable"] = True
        entry["content_type"] = kwargs.get("content_type", "")
        del raw
        return entry
    except FetchError as exc:
        if len(PROBE_LOG) > before:
            entry = dict(PROBE_LOG[-1])
        else:
            entry = {"url": url, "source": source}
        entry["reachable"] = False
        entry["error"] = exc.reason
        entry["status"] = exc.status
        return entry


def reset_probe_log() -> None:
    PROBE_LOG.clear()


def get_probe_log() -> List[Dict[str, Any]]:
    return list(PROBE_LOG)


def utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def urlencode(params: Dict[str, Any]) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
