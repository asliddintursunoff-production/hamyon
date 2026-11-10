"""Public HTTPS base URL for the Telegram Mini App.

Telegram only opens Mini Apps over HTTPS. Either set PUBLIC_BASE_URL (your own domain), or start the
optional Cloudflare quick tunnel (`docker compose --profile tunnel up -d`), whose current URL is
discovered automatically from its metrics endpoint.
"""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


async def resolve_webapp_base(settings) -> str | None:
    if settings.public_base_url and settings.public_base_url.startswith("https://"):
        return settings.public_base_url.rstrip("/")
    if settings.railway_public_domain:
        return f"https://{settings.railway_public_domain.strip().rstrip('/')}"
    if not settings.tunnel_metrics_url or settings.railway_service_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(settings.tunnel_metrics_url)
            host = (r.json() or {}).get("hostname") if r.status_code == 200 else None
    except Exception:
        return None  # tunnel not running
    if host and host.endswith(".trycloudflare.com"):
        return f"https://{host}"
    return None
