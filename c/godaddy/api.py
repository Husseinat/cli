"""Tiny GoDaddy Domains API client (stdlib-only).

Auth: `Authorization: sso-key <KEY>:<SECRET>`.
Credentials are read from `GODADDY_API_KEY` / `GODADDY_API_SECRET` env vars.
Generate keys at https://developer.godaddy.com/keys (make sure they're
"Production" keys, not OTE).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

BASE_URL = "https://api.godaddy.com"


class GoDaddyError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _creds() -> tuple[str, str]:
    key = os.environ.get("GODADDY_API_KEY", "").strip()
    secret = os.environ.get("GODADDY_API_SECRET", "").strip()
    if not key or not secret:
        from c.godaddy.configure import load
        cfg = load()
        key = key or str(cfg.get("api_key", "")).strip()
        secret = secret or str(cfg.get("api_secret", "")).strip()
    if not key or not secret:
        raise GoDaddyError(
            "GoDaddy credentials not found. Run `c godaddy configure` or set "
            "GODADDY_API_KEY and GODADDY_API_SECRET. "
            "Generate keys at https://developer.godaddy.com/keys (Production, not OTE)."
        )
    return key, secret


def request(method: str, path: str, body: Any | None = None) -> Any:
    key, secret = _creds()
    headers = {
        "Authorization": f"sso-key {key}:{secret}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            detail = json.loads(raw)
        except Exception:
            detail = raw
        raise GoDaddyError(
            f"GoDaddy API {method} {path} → {e.code}: {detail}", status=e.code
        ) from None
    except urllib.error.URLError as e:
        raise GoDaddyError(f"GoDaddy API {method} {path} failed: {e.reason}") from None
