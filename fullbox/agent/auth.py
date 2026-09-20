from __future__ import annotations

import hashlib
import secrets


def secret_digest(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def generate_enrollment_code() -> str:
    # 144 bits of entropy; the plaintext is returned only when it is created.
    return secrets.token_urlsafe(18)


def generate_device_token() -> str:
    # 256 bits of entropy; only the digest is persisted.
    return secrets.token_urlsafe(32)


def device_token_matches(agent_id: str, token: str) -> bool:
    agent_id = str(agent_id or "").strip()
    token = str(token or "").strip()
    if not agent_id or not token or len(token) > 256:
        return False

    # Import lazily to keep this low-level helper free of model import cycles.
    from .models import DeviceAgent

    stored_digest, revoked_at = (
        DeviceAgent.objects.filter(agent_id=agent_id)
        .values_list("token_digest", "token_revoked_at")
        .first()
        or ("", None)
    )
    if not stored_digest or revoked_at is not None:
        return False
    return secrets.compare_digest(stored_digest, secret_digest(token))
