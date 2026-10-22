"""Read-only MCP image resources. Image bytes never enter the agent transcript."""

import base64
import binascii
import io
import re

from PIL import Image

MAX_PREVIEW_BYTES = 20_000_000
PREVIEW_MEDIA_TYPES = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP"}


def preview_uri(asset_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", asset_id):
        raise ValueError("Invalid image asset identifier")
    return f"imagelab://assets/{asset_id}/preview"


def decode_preview(result: dict, uri: str) -> tuple[bytes, str]:
    contents = result.get("contents")
    if not isinstance(contents, list):
        raise ValueError("Missing image resource contents")
    for item in contents:
        if not isinstance(item, dict) or item.get("uri") != uri:
            continue
        media_type = item.get("mimeType") or item.get("mime_type")
        blob = item.get("blob")
        if media_type not in PREVIEW_MEDIA_TYPES or not isinstance(blob, str):
            raise ValueError("Unsupported image resource")
        if len(blob) > 4 * ((MAX_PREVIEW_BYTES + 2) // 3):
            raise ValueError("Image preview exceeds size limit")
        try:
            data = base64.b64decode(blob, validate=True)
            if not data or len(data) > MAX_PREVIEW_BYTES:
                raise ValueError("Invalid image preview size")
            with Image.open(io.BytesIO(data)) as image:
                if image.format != PREVIEW_MEDIA_TYPES[media_type]:
                    raise ValueError("Image media type mismatch")
                if max(image.size) > 4096 or min(image.size) < 1:
                    raise ValueError("Image preview exceeds dimension limit")
                image.verify()
        except (binascii.Error, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("Invalid image preview") from exc
        return data, media_type
    raise ValueError("Requested image resource was not returned")
