"""Minimal HS256 JWT, stdlib only.

The client receives a token inside ``session_url`` and hands it straight back
on the SSE connection; the PHP reference never verified the signature, but we
do, so a leaked URL cannot be replayed with a forged player id.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

_ALG = "HS256"


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class Signer:
    def __init__(self, secret: bytes | None = None) -> None:
        self.secret = secret or secrets.token_bytes(32)

    def encode(self, payload: dict) -> str:
        header = b64url(json.dumps({"alg": _ALG, "typ": "JWT"},
                                   separators=(",", ":")).encode())
        body = b64url(json.dumps(payload, separators=(",", ":")).encode())
        signing_input = f"{header}.{body}".encode("ascii")
        sig = hmac.new(self.secret, signing_input, hashlib.sha256).digest()
        return f"{header}.{body}.{b64url(sig)}"

    def decode(self, token: str, verify: bool = True) -> dict | None:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header, body, sig = parts
        if verify:
            expected = hmac.new(
                self.secret, f"{header}.{body}".encode("ascii"), hashlib.sha256
            ).digest()
            try:
                given = b64url_decode(sig)
            except Exception:  # noqa: BLE001
                return None
            if not hmac.compare_digest(expected, given):
                return None
        try:
            payload = json.loads(b64url_decode(body))
        except Exception:  # noqa: BLE001
            return None
        if verify and "exp" in payload and int(payload["exp"]) < int(time.time()):
            return None
        return payload


signer = Signer()
