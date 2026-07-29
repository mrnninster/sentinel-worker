"""Spawn Heroku one-off dynos (WallFly-style Platform API)."""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger(__name__)

HEROKU_ERROR_CAPACITY = "cannot_run_above_limit"


def _heroku_api_key() -> str:
    return (os.environ.get("HEROKU_API_KEY") or "").strip()


def _heroku_app_name() -> str:
    return (os.environ.get("HEROKU_APP_NAME") or "").strip()


def _dyno_size() -> str:
    return (os.environ.get("HEROKU_DYNO_SIZE") or "standard-1x").strip()


def heroku_configured() -> bool:
    return bool(_heroku_api_key() and _heroku_app_name())


async def create_oneoff_dyno(
    *,
    command: str,
    env: dict[str, str],
    size: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """
    POST /apps/{app}/dynos — one job per dyno.

    Returns dyno JSON on 201, or None on failure/capacity.
    """
    api_key = _heroku_api_key()
    app_name = _heroku_app_name()
    if not api_key or not app_name:
        log.error("HEROKU_API_KEY / HEROKU_APP_NAME not configured")
        return None

    payload = {
        "command": command,
        "type": "run",
        "size": size or _dyno_size(),
        "env": {k: str(v) for k, v in env.items() if v is not None},
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/vnd.heroku+json; version=3",
        "Authorization": f"Bearer {api_key}",
    }
    url = f"https://api.heroku.com/apps/{app_name}/dynos"

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            try:
                data = resp.json()
            except Exception:
                data = None

            if resp.status_code == 201 and isinstance(data, dict):
                log.info(
                    "Started one-off dyno id=%s name=%s size=%s cmd=%s",
                    data.get("id"),
                    data.get("name"),
                    payload["size"],
                    command,
                )
                return data

            if isinstance(data, dict) and data.get("id") == HEROKU_ERROR_CAPACITY:
                log.warning("Heroku app %s at one-off capacity", app_name)
                return None

            log.error(
                "Heroku dyno create failed status=%s body=%s",
                resp.status_code,
                (resp.text or "")[:500],
            )
            return None
    except Exception:
        log.exception("Heroku dyno create error")
        return None


async def kill_oneoff_dyno(dyno_id: str) -> bool:
    api_key = _heroku_api_key()
    app_name = _heroku_app_name()
    if not api_key or not app_name or not dyno_id:
        return False
    headers = {
        "Accept": "application/vnd.heroku+json; version=3",
        "Authorization": f"Bearer {api_key}",
    }
    url = f"https://api.heroku.com/apps/{app_name}/dynos/{dyno_id}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(url, headers=headers)
            ok = resp.status_code in {200, 202, 204}
            if ok:
                log.info("Killed one-off dyno %s", dyno_id)
            else:
                log.warning(
                    "Kill dyno %s → %s %s",
                    dyno_id,
                    resp.status_code,
                    (resp.text or "")[:200],
                )
            return ok
    except Exception:
        log.exception("Kill dyno failed id=%s", dyno_id)
        return False
