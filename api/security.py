"""API-key hashing and webhook signing helpers.

API keys are high-entropy random tokens, so they are hashed at rest with SHA-256.
For a random 256-bit secret SHA-256 is secure against brute force *and* deterministic,
which lets the service look a key up by its hash in one indexed query instead of
verifying against every stored key (as a salted password hash would force). This is
the "sha256" option the plan explicitly allows for API keys.

Webhook payloads are signed with an HMAC-SHA256 over ``"{timestamp}.{body}"`` so a
receiver can both verify authenticity and reject replays by checking the timestamp.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

API_KEY_PREFIX = "gdk_"
SIGNATURE_HEADER = "X-Glassdoor-Signature"
TIMESTAMP_HEADER = "X-Glassdoor-Timestamp"


def generate_api_key() -> str:
    """Generate a new opaque API key (shown to the operator exactly once)."""
    return f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"


def hash_api_key(plaintext: str) -> str:
    """Return the SHA-256 hex digest used to store/look up an API key."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def sign_webhook(secret: str, timestamp: str, body: bytes) -> str:
    """Return the hex HMAC-SHA256 signature for a webhook delivery."""
    message = timestamp.encode("utf-8") + b"." + body
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_webhook(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """Constant-time verification of a webhook signature (used in tests/consumers)."""
    expected = sign_webhook(secret, timestamp, body)
    return hmac.compare_digest(expected, signature)
