"""HTML -> pet records (name + demand only).

What this parser is built on
----------------------------
The only thing verified about https://amvgg.com/values/pets is its *rendered
text*: every pet appears as

    <Name> Value <number> Demand <★/☆ stars> F R N M <relative time>

and the page ends with "Showing all <N> pets". The real HTML markup (classes,
ids, scripts, JSON blobs) could NOT be inspected when this was written, so the
parser deliberately does not rely on any CSS class or element name. It finds the
smallest element whose visible text matches the pattern above.

The "Value <number>" part is still needed to *recognise* a card, but the number
itself is not read or stored. Each record contains only:

    id, name, demand ("3/5"), demand_stars (3)

Run `python scraper.py --inspect` to see the real markup and extend this file.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

import config

# Verified rendered-text pattern for one pet card (see module docstring).
CARD_RE = re.compile(
    r"^(?P<name>.+?)\s+Value\s+(?P<value>\S+)\s+Demand\s*"
    r"(?P<demand>[★☆\s]*)(?:F\s+R\s+N\s+M\b)?(?P<tail>.*)$",
    re.DOTALL,
)
# If the tail still contains another "Value x Demand", the element wraps >1 card.
_SECOND_CARD_RE = re.compile(r"\bValue\s+\S+\s+Demand\b")
_DECLARED_TOTAL_RE = re.compile(r"Showing all (\d[\d,]*) pets", re.IGNORECASE)


@dataclass
class ParseResult:
    pets: list[dict]
    declared_total: int | None
    warnings: list[str] = field(default_factory=list)
    candidates: int = 0


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "pet"


def _parse_demand(raw: str) -> tuple[str | None, int | None]:
    filled, empty = raw.count("★"), raw.count("☆")
    if filled + empty == 0:
        return None, None
    return f"{filled}/{filled + empty}", filled


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup


# --------------------------------------------------------------------------- #
# Card discovery
# --------------------------------------------------------------------------- #
def _find_cards(soup: BeautifulSoup) -> list[tuple[Tag, re.Match]]:
    """Return (card_element, regex_match) for every pet found, in page order."""
    matched: list[tuple[Tag, re.Match]] = []
    for el in soup.find_all(True):
        if el.name in ("html", "body", "head"):
            continue
        text = _norm(el.get_text(" ", strip=True))
        if not text or len(text) > config.MAX_CARD_TEXT:
            continue
        m = CARD_RE.match(text)
        if not m:
            continue
        name = m.group("name").strip()
        if "Demand" in name or len(name) > config.MAX_NAME_LEN:
            continue
        if _SECOND_CARD_RE.search(m.group("tail")):
            continue
        matched.append((el, m))

    # Keep only the innermost matching elements (drop wrappers with same text).
    ids = {id(el) for el, _ in matched}
    innermost = [
        (el, m)
        for el, m in matched
        if not any(isinstance(d, Tag) and id(d) in ids for d in el.descendants)
    ]

    # Climb to the largest ancestor that still contains exactly one pet.
    counts: dict[int, int] = {}
    for el, _ in innermost:
        for anc in el.parents:
            counts[id(anc)] = counts.get(id(anc), 0) + 1
    cards: list[tuple[Tag, re.Match]] = []
    for el, m in innermost:
        card = el
        while card.parent is not None and counts.get(id(card.parent), 0) == 1:
            card = card.parent
        cards.append((card, m))
    return cards


def parse_page(html: str, base_url: str = config.SOURCE_URL) -> ParseResult:
    soup = _clean_soup(html)
    page_text = _norm(soup.get_text(" ", strip=True))

    declared = None
    dm = _DECLARED_TOTAL_RE.search(page_text)
    if dm:
        declared = int(dm.group(1).replace(",", ""))

    warnings: list[str] = []
    cards = _find_cards(soup)
    pets: list[dict] = []
    seen: dict[str, int] = {}

    for _card, m in cards:
        name = _norm(m.group("name"))
        demand, stars = _parse_demand(m.group("demand"))

        pid = slugify(name)
        seen[pid] = seen.get(pid, 0) + 1
        if seen[pid] > 1:
            warnings.append(f"duplicate pet name {name!r}; id suffixed")
            pid = f"{pid}-{seen[pid]}"

        if stars is None:
            warnings.append(f"{name!r}: no demand rating found -> null")

        pets.append(
            {
                "id": pid,
                "name": name,
                "demand": demand,        # e.g. "3/5"
                "demand_stars": stars,   # e.g. 3
            }
        )

    return ParseResult(pets=pets, declared_total=declared, warnings=warnings, candidates=len(cards))


# --------------------------------------------------------------------------- #
# Record-level validation
# --------------------------------------------------------------------------- #
def validate_records(pets: list[dict]) -> list[str]:
    errors: list[str] = []
    if not pets:
        return ["no pets parsed"]
    ids = [p["id"] for p in pets]
    if len(ids) != len(set(ids)):
        errors.append("duplicate ids in parsed pets")
    for p in pets:
        if not p.get("name"):
            errors.append(f"pet {p.get('id')!r} has empty name")
    with_demand = sum(1 for p in pets if p.get("demand_stars") is not None)
    if with_demand / len(pets) < config.MIN_VALUE_COVERAGE:
        errors.append(
            f"only {with_demand}/{len(pets)} pets have a demand rating "
            f"(< {config.MIN_VALUE_COVERAGE:.0%}); page structure may have changed"
        )
    return errors


# --------------------------------------------------------------------------- #
# Diagnostics: `python scraper.py --inspect`
# --------------------------------------------------------------------------- #
_JSON_HINTS = ("__NEXT_DATA__", "__NUXT__", "self.__next_f", "window.__", "application/json")


_FLIGHT_RE = re.compile(r"^\s*self\.__next_f\.push\((\[.*\])\)\s*;?\s*$", re.DOTALL)


def _flight_text(raw: BeautifulSoup) -> str:
    """Concatenate Next.js `self.__next_f.push([1,"..."])` string chunks (the page's embedded data)."""
    parts: list[str] = []
    for s in raw.find_all("script"):
        if s.get("src"):
            continue
        m = _FLIGHT_RE.match(s.string or s.get_text() or "")
        if not m:
            continue
        try:
            arr = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(arr, list) and len(arr) >= 2 and arr[0] == 1 and isinstance(arr[1], str):
            parts.append(arr[1])
    return "".join(parts)


def inspect_html(html: str, base_url: str = config.SOURCE_URL) -> str:
    """Human-readable report of the page's real structure, for extending the parser."""
    raw = BeautifulSoup(html, "html.parser")
    out: list[str] = []
    out.append(f"HTML length: {len(html)} chars")
    out.append(f"<title>: {raw.title.get_text(strip=True) if raw.title else None}")

    scripts = raw.find_all("script")
    out.append(f"\n== <script> tags: {len(scripts)} ==")
    for s in scripts:
        if s.get("src"):
            out.append(f"  src: {s['src']}")
    out.append("\n== Inline scripts that look like data (first 300 chars) ==")
    found = False
    for s in scripts:
        if s.get("src"):
            continue
        txt = s.string or s.get_text() or ""
        stype = (s.get("type") or "").lower()
        if "json" in stype or txt.lstrip().startswith(("{", "[")) or any(h in txt for h in _JSON_HINTS):
            found = True
            out.append(f"  id={s.get('id')!r} type={stype!r} length={len(txt)}")
            out.append("    " + _norm(txt[:300]))
    if not found:
        out.append("  (none)")

    imgs = raw.find_all("img")
    out.append(f"\n== <img> tags: {len(imgs)} (lazy: {sum(1 for i in imgs if i.get('loading') == 'lazy' or i.get('data-src'))}) ==")
    for i in imgs[:5]:
        out.append(f"  {dict(i.attrs)}")

    soup = _clean_soup(html)
    cards = _find_cards(soup)
    page_text = _norm(soup.get_text(" ", strip=True))
    dm = _DECLARED_TOTAL_RE.search(page_text)
    out.append(f"\n== Pet cards found by text pattern: {len(cards)} ==")
    out.append(f"Declared total on page: {dm.group(0) if dm else 'NOT FOUND'}")
    for idx, (card, _) in enumerate(cards[:2], 1):
        out.append(f"\n-- Card {idx}: <{card.name}> attrs={dict(card.attrs)}")
        out.append(card.prettify()[:9000])
        links = [a.get("href") for a in card.find_all("a")]
        out.append(f"links inside card: {links}")
    if not cards:
        out.append(
            "No cards matched. The page may be JS-rendered, blocked, or restructured.\n"
            "First 1500 chars of visible text:\n" + page_text[:1500]
        )

    # Where does the page keep its data? Show the embedded Next.js payload around
    # the first pets so other fields (if present) can be located.
    out.append("\n== Embedded page data (Next.js payload) ==")
    try:
        flight = _flight_text(raw)
        out.append(f"Decoded payload length: {len(flight)} chars")
        names = [m.group("name").strip() for _, m in cards[:2]]
        shown = 0
        for name in names:
            idx = flight.find(name)
            out.append(f"\n-- First occurrence of {name!r} at index {idx}")
            if idx >= 0:
                out.append(flight[max(0, idx - 700): idx + 2300])
                shown += 1
        if shown == 0 and flight:
            out.append("Pet names not found in payload; first 3000 chars:\n" + flight[:3000])
        if not flight:
            out.append("(no Next.js payload chunks found)")
    except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
        out.append(f"(payload extraction failed: {exc})")
    return "\n".join(out)
