"""Deployment-owned ImageLab credentials, keyed only by Fruitcake user ID."""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import SecretStr

IMAGELAB_SERVER = "comfyui_image_lab"
_ERROR = "IMAGELAB_USER_TOKENS_JSON must map positive Fruitcake user IDs to nonempty bearer tokens"


def parse_user_tokens(value: SecretStr) -> dict[int, SecretStr]:
    raw = value.get_secret_value()
    if len(raw) > 262_144:
        raise ValueError(_ERROR)

    def unique_object(pairs):
        result = {}
        for key, token in pairs:
            if key in result:
                raise ValueError(_ERROR)
            result[key] = token
        return result

    try:
        entries = json.loads(raw, object_pairs_hook=unique_object)
    except (ValueError, RecursionError):
        raise ValueError(_ERROR) from None
    if not isinstance(entries, dict) or len(entries) > 256:
        raise ValueError(_ERROR)
    tokens = {}
    for user_id, token in entries.items():
        if (
            not re.fullmatch(r"[1-9][0-9]{0,18}", user_id)
            or int(user_id) > 2**63 - 1
            or not isinstance(token, str)
            or not 1 <= len(token) <= 8192
            or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token)
        ):
            raise ValueError(_ERROR)
        tokens[int(user_id)] = SecretStr(token)
    return tokens


def shared_config(config: dict[str, Any]) -> dict[str, Any]:
    """Never inherit deployment-wide Authorization into tokenless Shared access."""
    result = dict(config)
    for key in ("headers", "headers_from_env"):
        result[key] = {
            name: value for name, value in (config.get(key) or {}).items()
            if name.lower() != "authorization"
        }
    return result


def context_user_id(context: Any) -> int | None:
    if isinstance(context, dict):
        candidates = [context.get("user_id")]
    else:
        candidates = [getattr(context, "user_id", None), getattr(context, "id", None)]
    return next((value for value in candidates if type(value) is int and value > 0), None)
