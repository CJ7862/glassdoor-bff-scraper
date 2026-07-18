"""HMAC-signed outbound webhook delivery.

When a search finishes and the submitter supplied a ``webhook_url``, the completed
payload is POSTed there with an HMAC-SHA256 signature header so the receiver can
verify authenticity. Delivery is retried a bounded number of times with exponential
backoff; a 2xx response is success, anything else (or a transport error) is retried
until attempts are exhausted.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from .security import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign_webhook

log = logging.getLogger("api.webhooks")


async def deliver_webhook(
    url: str,
    payload: dict,
    *,
    secret: str,
    timeout: float = 10.0,
    max_attempts: int = 3,
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Deliver ``payload`` to ``url``, signed, with bounded retries.

    Returns True if the receiver acknowledged with a 2xx status.
    """
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout)

    try:
        for attempt in range(1, max_attempts + 1):
            timestamp = str(int(asyncio.get_event_loop().time() * 1000))
            headers = {
                "content-type": "application/json",
                TIMESTAMP_HEADER: timestamp,
            }
            if secret:
                headers[SIGNATURE_HEADER] = sign_webhook(secret, timestamp, body)

            try:
                resp = await client.post(url, content=body, headers=headers)
                if 200 <= resp.status_code < 300:
                    log.info(
                        "Webhook delivered to %s (HTTP %d, attempt %d).",
                        url,
                        resp.status_code,
                        attempt,
                    )
                    return True
                log.warning(
                    "Webhook to %s returned HTTP %d (attempt %d/%d).",
                    url,
                    resp.status_code,
                    attempt,
                    max_attempts,
                )
            except httpx.HTTPError as exc:
                log.warning(
                    "Webhook to %s failed (attempt %d/%d): %s",
                    url,
                    attempt,
                    max_attempts,
                    exc,
                )

            if attempt < max_attempts:
                await asyncio.sleep(min(2 ** (attempt - 1), 10))

        log.error("Webhook delivery to %s failed after %d attempts.", url, max_attempts)
        return False
    finally:
        if owns_client:
            await client.aclose()
