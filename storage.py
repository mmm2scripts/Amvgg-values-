"""Reading and writing data/pets.json safely."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

import config

log = logging.getLogger("amvgg.storage")


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def content_hash(pets: list[dict]) -> str:
    """Order-independent hash of the pet records (used for change detection)."""
    canonical = json.dumps(
        sorted(pets, key=lambda p: p["id"]),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_payload(pets: list[dict], now: datetime) -> dict:
    return {
        "source": {"name": config.SOURCE_NAME, "url": config.SOURCE_URL},
        # Time the *content* last changed. Deliberately not bumped on unchanged
        # runs so that unchanged scrapes produce no git diff and no commit.
        "last_updated": iso(now),
        "total_pets": len(pets),
        "content_hash": content_hash(pets),
        "pets": pets,
    }


def save_payload(payload: dict, path: Path | None = None) -> None:
    """Atomic write: temp file in the same directory, then os.replace."""
    path = path or config.PETS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pets-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_pets(path: Path | None = None) -> dict | None:
    """Return the previous payload, or None if missing, corrupt, or empty."""
    path = path or config.PETS_FILE
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Existing %s is unreadable (%s); treating as no previous data", path, exc)
        return None
    if not isinstance(payload, dict) or not payload.get("pets"):
        return None
    return payload
