"""
FruitcakeAI v5 — APNs Pusher (Sprint 4.3)

Delivers task results to Apple devices via APNs HTTP/2.

JWT signing:  ES256 with the Apple-provided .p8 key file.
Token caching: Tokens are valid ≤ 60 min; reuse up to 50 min to stay
               under Apple's rate limit and avoid repeated key-file I/O.

Required environment variables:
    APNS_KEY_ID          — 10-char Key ID from developer.apple.com/account/resources/authkeys
    APNS_TEAM_ID         — 10-char Team ID (top-right on developer.apple.com)
    APNS_AUTH_KEY_PATH   — absolute path to AuthKey_<key_id>.p8
    APNS_BUNDLE_ID       — app bundle ID (e.g. "none.FruitcakeAi")
    APNS_ENVIRONMENT     — "sandbox" | "production"  (default: sandbox)

Leave all APNS_ vars empty to run without push (tasks still complete, no delivery).
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# APNs gateway hosts
_APNS_HOST: dict[str, str] = {
    "production": "api.push.apple.com",
    "sandbox":    "api.sandbox.push.apple.com",
}

# Refresh the JWT 10 min before Apple's 60-min hard limit
_JWT_TTL_SECS = 50 * 60


# ── JWT builder ───────────────────────────────────────────────────────────────

def _make_jwt(key_id: str, team_id: str, key_path: str) -> str:
    """Build and sign an ES256 APNs provider token from the .p8 key file."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    from cryptography.hazmat.primitives.hashes import SHA256

    pem = Path(key_path).read_bytes()
    private_key = load_pem_private_key(pem, password=None)

    header  = {"alg": "ES256", "kid": key_id}
    payload = {"iss": team_id, "iat": int(time.time())}

    def _b64url(data: bytes | str) -> str:
        if isinstance(data, str):
            data = data.encode()
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    h = _b64url(json.dumps(header,  separators=(",", ":")))
    p = _b64url(json.dumps(payload, separators=(",", ":")))
    signing_input = f"{h}.{p}".encode()

    # ES256: sign → DER → raw (r || s) for JWT
    sig_der = private_key.sign(signing_input, ECDSA(SHA256()))
    r, s = decode_dss_signature(sig_der)
    sig = _b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    return f"{h}.{p}.{sig}"


# ── Pusher ────────────────────────────────────────────────────────────────────

class APNsPusher:
    """
    Sends APNs push notifications to registered Apple devices.

    The singleton is safe to reuse across task runs: it caches the signed JWT
    and refreshes it automatically when it approaches expiry.
    """

    def __init__(self) -> None:
        from app.config import settings
        self._key_id   = settings.apns_key_id
        self._team_id  = settings.apns_team_id
        self._key_path = settings.apns_auth_key_path
        self._bundle_id = settings.apns_bundle_id
        self._default_env = settings.apns_environment
        # Cached JWT: (token_str, issued_at unix timestamp)
        self._jwt: Optional[tuple[str, float]] = None

    @property
    def _configured(self) -> bool:
        return bool(
            self._key_id and self._team_id
            and self._key_path and self._bundle_id
        )

    def _get_jwt(self) -> str:
        """Return a valid provider JWT, rebuilding it if near expiry."""
        now = time.time()
        if self._jwt is None or (now - self._jwt[1]) >= _JWT_TTL_SECS:
            token = _make_jwt(self._key_id, self._team_id, self._key_path)
            self._jwt = (token, now)
            log.debug("apns.jwt_refreshed")
        return self._jwt[0]

    async def send(
        self,
        device_token: str,
        environment: str,
        title: str,
        body: str,
    ) -> bool:
        """
        Push a notification to one device token.  Returns True on success.

        Failures are logged but never raised — push is best-effort and must
        not affect task status.

        APNs status codes handled:
            200 — delivered
            410 — device token expired/unregistered → pruned from DB
            other → logged as warning
        """
        if not self._configured:
            log.warning(
                "apns.not_configured",
                hint="Set APNS_KEY_ID / APNS_TEAM_ID / APNS_AUTH_KEY_PATH / APNS_BUNDLE_ID",
            )
            return False

        env  = environment if environment in _APNS_HOST else self._default_env
        host = _APNS_HOST[env]
        url  = f"https://{host}/3/device/{device_token}"

        apns_payload = {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": "default",
            }
        }
        headers = {
            "authorization": f"bearer {self._get_jwt()}",
            "apns-topic":    self._bundle_id,
            "apns-push-type": "alert",
            "apns-priority":  "10",
        }

        try:
            import httpx
            async with httpx.AsyncClient(http2=True, timeout=10) as client:
                resp = await client.post(url, json=apns_payload, headers=headers)

            if resp.status_code == 200:
                log.info("apns.delivered", prefix=device_token[:8], env=env)
                return True

            if resp.status_code == 410:
                # Token permanently invalid — remove from DB so we don't retry
                log.info("apns.token_expired", prefix=device_token[:8])
                await self._expire_token(device_token)
                return False

            log.warning(
                "apns.rejected",
                status=resp.status_code,
                reason=resp.text[:200],
                prefix=device_token[:8],
            )
            return False

        except Exception as exc:
            log.error("apns.send_error", error=str(exc), prefix=device_token[:8])
            return False

    async def _expire_token(self, device_token: str) -> None:
        """Delete a 410-expired token from the DB to prevent repeated failures."""
        try:
            from app.db.session import AsyncSessionLocal
            from app.db.models import DeviceToken
            from sqlalchemy import delete

            async with AsyncSessionLocal() as db:
                await db.execute(
                    delete(DeviceToken).where(DeviceToken.token == device_token)
                )
                await db.commit()
            log.info("apns.token_pruned", prefix=device_token[:8])
        except Exception as exc:
            log.error("apns.prune_error", error=str(exc))


# ── Singleton ─────────────────────────────────────────────────────────────────

_pusher: Optional[APNsPusher] = None


def get_apns_pusher() -> APNsPusher:
    global _pusher
    if _pusher is None:
        _pusher = APNsPusher()
    return _pusher
