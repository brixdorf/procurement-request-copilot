from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import requests

DEFAULT_BASE_URL = "http://127.0.0.1:8001"
REQUIRED_FIELDS = (
    "risk_level",
    "security_review_status",
    "last_review_date",
    "processes_personal_data",
    "stores_data_outside_region",
)


def _base_url() -> str:
    return os.getenv("VENDOR_RISK_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def get_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    """Low-level API client (raises on any HTTP or network error)."""
    url = f"{_base_url()}/vendor-risk/{quote(vendor_name, safe='')}"
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


@dataclass
class VendorRiskResult:
    """Outcome of a vendor-risk lookup, including failures.

    status is one of:
      ok           - record returned and validated
      not_found    - service reachable, no record for this vendor (404)
      unavailable  - timeout, connection error or 5xx after bounded retries
      invalid      - 200 response whose body is not a usable record
    """

    status: str
    data: dict | None = None
    http_status: int | None = None
    message: str = ""
    attempts: int = 1

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def fetch_vendor_risk(
    vendor_name: str,
    timeout_seconds: float = 3.0,
    max_attempts: int = 3,
    backoff_seconds: float = 0.3,
) -> VendorRiskResult:
    """Fetch vendor risk with a timeout, bounded retries and exponential backoff.

    Retries only transient failures (network errors, 5xx). A 404 or other 4xx is
    returned immediately because repeating it cannot change the answer.
    """
    last = VendorRiskResult(status="unavailable", message="not attempted", attempts=0)
    for attempt in range(1, max_attempts + 1):
        try:
            url = f"{_base_url()}/vendor-risk/{quote(vendor_name, safe='')}"
            response = requests.get(url, timeout=timeout_seconds)
        except requests.RequestException as exc:
            last = VendorRiskResult(
                status="unavailable",
                message=f"{type(exc).__name__}: {str(exc)[:120]}",
                attempts=attempt,
            )
        else:
            code = response.status_code
            if code == 200:
                try:
                    body = response.json()
                except ValueError:
                    return VendorRiskResult("invalid", None, code, "response was not valid JSON", attempt)
                if not isinstance(body, dict) or any(f not in body for f in REQUIRED_FIELDS):
                    return VendorRiskResult("invalid", None, code, "response is missing required fields", attempt)
                return VendorRiskResult("ok", body, code, "", attempt)
            detail = _detail(response)
            if code == 404:
                return VendorRiskResult("not_found", None, code, detail, attempt)
            if 400 <= code < 500:
                return VendorRiskResult("invalid", None, code, detail, attempt)
            last = VendorRiskResult("unavailable", None, code, detail, attempt)
        if attempt < max_attempts:
            time.sleep(backoff_seconds * (2 ** (attempt - 1)))
    return last


def _detail(response: requests.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])[:200]
    except ValueError:
        pass
    return f"HTTP {response.status_code}"


# ---------------------------------------------------------------------------
# Local convenience: start the mock service in-process when it is not running.
# ---------------------------------------------------------------------------
_autostart_lock = threading.Lock()
_autostarted = False


def _healthy(base: str) -> bool:
    try:
        return requests.get(f"{base}/health", timeout=0.6).ok
    except requests.RequestException:
        return False


def ensure_local_vendor_api() -> None:
    """Start the bundled mock vendor-risk API in a background thread if needed.

    Only applies to a localhost base URL. An outage is still simulated by the
    service itself (HTTP 503 records), so this never masks a real failure of a
    remote service. Disable with VENDOR_RISK_AUTOSTART=0.
    """
    global _autostarted
    if os.getenv("VENDOR_RISK_AUTOSTART", "1") == "0":
        return
    base = _base_url()
    parsed = urlparse(base)
    if parsed.hostname not in ("127.0.0.1", "localhost") or not parsed.port:
        return
    with _autostart_lock:
        if _autostarted or _healthy(base):
            return
        try:
            import uvicorn

            from mock_api.app import app
        except ImportError:
            return
        config = uvicorn.Config(app, host=parsed.hostname, port=parsed.port, log_level="warning")
        server = uvicorn.Server(config)
        threading.Thread(target=server.run, daemon=True, name="mock-vendor-api").start()
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if _healthy(base):
                break
            time.sleep(0.1)
        _autostarted = True
