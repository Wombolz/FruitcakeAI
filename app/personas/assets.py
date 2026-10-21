"""Owner-only image references; normalize pixels and never accept filesystem paths."""
import hashlib
import io
import warnings
import asyncio

from fastapi import HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import func, select

from app.db.models import PersonaVisualAsset

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_PIXELS = 4_000_000
MAX_OWNER_BYTES = 128 * 1024 * 1024
MAX_OWNER_ASSETS = 128


def normalize_image(data: bytes) -> tuple[bytes, int, int]:
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Reference images must be nonempty and at most 8 MiB.")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=["PNG", "JPEG", "WEBP"]) as image:
                if image.width * image.height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise HTTPException(413, "Use a still image of at most 4 megapixels.")
                image.load()
                oriented = ImageOps.exif_transpose(image)
                mode = "RGBA" if oriented.mode in ("RGBA", "LA") or "transparency" in oriented.info else "RGB"
                # A fresh pixel image strips EXIF, embedded profiles and textual metadata.
                pixels = oriented.convert(mode)
                clean = Image.new(mode, pixels.size)
                clean.paste(pixels)
                output = io.BytesIO()
                clean.save(output, format="PNG")
                normalized = output.getvalue()
                if len(normalized) > MAX_IMAGE_BYTES:
                    raise HTTPException(413, "Normalized image exceeds 8 MiB; choose a smaller image.")
                return normalized, clean.width, clean.height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, "Choose a valid PNG, JPEG or still WebP image.") from None


def metadata(asset):
    return {"id": asset.id, "sha256": asset.sha256, "width": asset.width,
            "height": asset.height, "size_bytes": asset.size_bytes, "media_type": "image/png"}


async def owned_asset(db, user, asset_id):
    row = (await db.execute(select(PersonaVisualAsset).where(
        PersonaVisualAsset.id == asset_id, PersonaVisualAsset.user_id == user.id))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Persona image not found")
    return row


async def store_image(db, user, data):
    from app.personas.service import audit, lock_owner
    normalized, width, height = await asyncio.to_thread(normalize_image, data)
    await lock_owner(db, user)
    count, size = (await db.execute(select(func.count(PersonaVisualAsset.id),
        func.coalesce(func.sum(PersonaVisualAsset.size_bytes), 0)).where(PersonaVisualAsset.user_id == user.id))).one()
    if count >= MAX_OWNER_ASSETS or size + len(normalized) > MAX_OWNER_BYTES:
        raise HTTPException(413, "Persona image storage limit reached (128 images / 128 MiB).")
    row = PersonaVisualAsset(user_id=user.id, sha256=hashlib.sha256(normalized).hexdigest(),
                            width=width, height=height, size_bytes=len(normalized), data=normalized)
    db.add(row)
    await db.flush()
    audit(db, user, "image_uploaded", asset_id=row.id)
    return metadata(row)
