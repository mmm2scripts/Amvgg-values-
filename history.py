"""Change detection and historical snapshots (pet name + demand only).

Snapshots live in data/history/ as <UTC timestamp>.json:
  * the first one is a "full" snapshot (the complete pet list = baseline)
  * later ones are "delta" snapshots (only what changed since the previous run)
A snapshot is written only when the content actually changed.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

from parser import slugify
from storage import iso

log = logging.getLogger("amvgg.history")

# Only demand is tracked. demand_stars is the numeric form ("3/5" -> 3), so a
# change in it is classed as an increase/decrease; the "demand" string is derived
# from it and not tracked separately (that would double-count every change).
TRACKED = ["demand_stars"]
_SNAP_RE = re.compile(r"^\d{8}T\d{6}Z(-\d+)?\.json$")


# --------------------------------------------------------------------------- #
# Diffing
# --------------------------------------------------------------------------- #
def _get(pet: dict, path: str):
    return pet.get(path)


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _describe(path: str, old, new) -> dict:
    change = {"field": path, "old": old, "new": new, "direction": "changed",
              "delta": None, "pct": None}
    if _is_num(old) and _is_num(new):
        change["direction"] = "up" if new > old else "down"
        change["delta"] = round(new - old, 10)
        change["pct"] = round((new - old) / old * 100, 2) if old else None
    return change


def diff_pets(old: list[dict], new: list[dict]) -> dict:
    old_by = {p["id"]: p for p in old}
    new_by = {p["id"]: p for p in new}

    added = [p for pid, p in new_by.items() if pid not in old_by]
    removed = [
        {"id": pid, "name": p["name"], "demand": p.get("demand")}
        for pid, p in old_by.items() if pid not in new_by
    ]
    changed = []
    for pid, np_ in new_by.items():
        op = old_by.get(pid)
        if op is None:
            continue
        changes = [
            _describe(path, _get(op, path), _get(np_, path))
            for path in TRACKED if _get(op, path) != _get(np_, path)
        ]
        if changes:
            changed.append({"id": pid, "name": np_["name"], "changes": changes})

    flat = [c for entry in changed for c in entry["changes"]]
    summary = {
        "added": len(added),
        "removed": len(removed),
        "pets_changed": len(changed),
        "demand_increases": sum(1 for c in flat if c["direction"] == "up"),
        "demand_decreases": sum(1 for c in flat if c["direction"] == "down"),
        "other_changes": sum(1 for c in flat if c["direction"] not in ("up", "down")),
    }
    return {"summary": summary, "added": added, "removed": removed, "changed": changed}


def has_changes(diff: dict) -> bool:
    return bool(diff["added"] or diff["removed"] or diff["changed"])


def format_diff(diff: dict, limit: int = 25) -> list[str]:
    s = diff["summary"]
    lines = [
        f"added={s['added']} removed={s['removed']} pets_changed={s['pets_changed']} "
        f"demand_increases={s['demand_increases']} demand_decreases={s['demand_decreases']} "
        f"other_changes={s['other_changes']}"
    ]
    for p in diff["added"][:limit]:
        lines.append(f"  + ADDED   {p['name']} (demand {p.get('demand')})")
    for p in diff["removed"][:limit]:
        lines.append(f"  - REMOVED {p['name']}")
    shown = 0
    for entry in diff["changed"]:
        for c in entry["changes"]:
            if shown >= limit:
                break
            arrow = {"up": "↑", "down": "↓"}.get(c["direction"], "~")
            pct = f" ({c['pct']:+}%)" if c["pct"] is not None else ""
            lines.append(f"  {arrow} {entry['name']}: {c['field']} {c['old']} -> {c['new']}{pct}")
            shown += 1
    return lines


# --------------------------------------------------------------------------- #
# Snapshots
# --------------------------------------------------------------------------- #
def _snap_key(path: Path) -> tuple[str, int]:
    """Sort by timestamp, then by collision suffix (…Z.json < …Z-1.json < …Z-2.json)."""
    stamp, _, suffix = path.stem.partition("-")
    return stamp, int(suffix) if suffix else 0


def list_snapshot_files(history_dir: Path) -> list[Path]:
    if not history_dir.exists():
        return []
    return sorted((p for p in history_dir.iterdir() if _SNAP_RE.match(p.name)), key=_snap_key)


def load_snapshots(history_dir: Path) -> list[dict]:
    out = []
    for path in list_snapshot_files(history_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Skipping unreadable snapshot %s: %s", path.name, exc)
            continue
        data["_file"] = path.name
        out.append(data)
    return out


def write_snapshot(history_dir: Path, payload: dict, previous: dict | None,
                   diff: dict, now: datetime) -> Path | None:
    """Write a snapshot unless the newest one already has this content hash."""
    history_dir.mkdir(parents=True, exist_ok=True)
    files = list_snapshot_files(history_dir)
    new_hash = payload["content_hash"]
    if files:
        try:
            last_hash = json.loads(files[-1].read_text(encoding="utf-8")).get("content_hash")
        except (OSError, json.JSONDecodeError):
            last_hash = None
        if last_hash == new_hash:
            log.info("Newest snapshot already has this content; not writing a duplicate")
            return None

    is_full = previous is None or not files
    snapshot = {
        "type": "full" if is_full else "delta",
        "timestamp": iso(now),
        "content_hash": new_hash,
        "previous_hash": previous.get("content_hash") if previous else None,
        "total_pets": payload["total_pets"],
        "summary": diff["summary"],
    }
    if is_full:
        snapshot["pets"] = payload["pets"]
    else:
        snapshot.update(added=diff["added"], removed=diff["removed"], changed=diff["changed"])

    name = now.strftime("%Y%m%dT%H%M%SZ") + ".json"
    path = history_dir / name
    n = 1
    while path.exists():
        path = history_dir / f"{name[:-5]}-{n}.json"
        n += 1
    path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("Wrote %s snapshot %s", snapshot["type"], path.name)
    return path


# --------------------------------------------------------------------------- #
# `python scraper.py --history`
# --------------------------------------------------------------------------- #
def _matches(query: str, pid: str, name: str) -> bool:
    return pid == slugify(query) or name.lower() == query.lower()


def format_history(history_dir: Path, limit: int = 10, pet: str | None = None) -> str:
    snaps = load_snapshots(history_dir)
    if not snaps:
        return f"No history snapshots in {history_dir} yet."

    if pet:
        lines = [f"History for {pet!r} across {len(snaps)} snapshot(s):"]
        found = False
        for s in snaps:
            ts = s.get("timestamp")
            if s.get("type") == "full":
                for p in s.get("pets", []):
                    if _matches(pet, p["id"], p["name"]):
                        found = True
                        lines.append(f"  {ts}  baseline: demand={p['demand']}")
                continue
            for p in s.get("added", []):
                if _matches(pet, p["id"], p["name"]):
                    found = True
                    lines.append(f"  {ts}  ADDED (demand {p.get('demand')})")
            for p in s.get("removed", []):
                if _matches(pet, p["id"], p["name"]):
                    found = True
                    lines.append(f"  {ts}  REMOVED")
            for entry in s.get("changed", []):
                if _matches(pet, entry["id"], entry["name"]):
                    found = True
                    for c in entry["changes"]:
                        pct = f" ({c['pct']:+}%)" if c.get("pct") is not None else ""
                        lines.append(f"  {ts}  {c['field']}: {c['old']} -> {c['new']}{pct}")
        if not found:
            lines.append("  (pet not found in any snapshot)")
        return "\n".join(lines)

    lines = [f"{len(snaps)} snapshot(s) in {history_dir}; showing the latest {min(limit, len(snaps))}:"]
    for s in snaps[-limit:]:
        sm = s.get("summary", {})
        lines.append(
            f"  {s.get('timestamp')}  {s.get('type'):5}  pets={s.get('total_pets')}  "
            f"+{sm.get('added', 0)} -{sm.get('removed', 0)}  "
            f"up={sm.get('demand_increases', 0)} down={sm.get('demand_decreases', 0)} "
            f"other={sm.get('other_changes', 0)}"
        )
    latest = snaps[-1]
    if latest.get("type") == "delta":
        lines.append("\nLatest changes:")
        lines.extend(format_diff(latest, limit=15))
    return "\n".join(lines)
