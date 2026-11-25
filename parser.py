"""HTML -> pet records.

What this parser is built on
----------------------------
The only thing verified about https://amvgg.com/values/pets is its *rendered
text*: every pet appears as

    <Name> Value <number> Demand <★/☆ stars> F R N M <relative time>

and the page ends with "Showing all <N> pets". The real HTML markup (classes,
ids, scripts, JSON blobs) could NOT be inspected when this was written, so the
parser deliberately does not rely on any CSS class or element name. It finds the
smallest element whose visible text matches the pattern above, then looks for an
<img> inside that card.

Run `python scraper.py --inspect` to see the real markup and extend this file.

Fields the list page does not expose (rarity, trend, fly/ride/fly_ride/neon/
mega_neon values) are always null. Nothing is guessed.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

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


def _parse_number(raw: str) -> float | None:
    try:
        value = float(raw.replace(",", "").strip())
    except ValueError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _parse_demand(raw: str) -> tuple[str | None, int | None]:
    filled, empty = raw.count("★"), raw.count("☆")
    if filled + empty == 0:
        return None, None
    return f"{filled}/{filled + empty}", filled


def _image_url(card: Tag, base_url: str) -> str | None:
    img = card.find("img")
    if img is None:
        return None
    candidates = [img.get("data-src"), img.get("data-lazy-src")]
    src = img.get("src")
    if src and not src.startswith("data:"):
        candidates.append(src)
    srcset = img.get("srcset") or img.get("data-srcset")
    if srcset:
        candidates.append(srcset.split(",")[0].strip().split(" ")[0])
    for cand in candidates:
        if cand and not cand.startswith("data:"):
            return urljoin(base_url, cand.strip())
    return None


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

    # Climb to the largest ancestor that still contains exactly one pet, so an
    # <img> sitting beside the text block is included in the card.
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

    for card, m in cards:
        name = _norm(m.group("name"))
        raw_value = m.group("value")
        value = _parse_number(raw_value)
        if value is None:
            warnings.append(f"{name!r}: unparseable value {raw_value!r} -> null")
        demand, stars = _parse_demand(m.group("demand"))

        pid = slugify(name)
        seen[pid] = seen.get(pid, 0) + 1
        if seen[pid] > 1:
            warnings.append(f"duplicate pet name {name!r}; id suffixed")
            pid = f"{pid}-{seen[pid]}"

        values = {k: None for k in config.VALUE_FIELDS}
        if config.ASSUME_LISTED_VALUE_IS_REGULAR:
            values["regular"] = value

        pets.append(
            {
                "id": pid,
                "name": name,
                "rarity": None,        # not present on the list page
                "listed_value": value,  # exactly the number the page labels "Value"
                "values": values,
                "demand": demand,
                "demand_stars": stars,
                "trend": None,         # not present on the list page
                "image_url": _image_url(card, base_url),
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
        for k, v in (p.get("values") or {}).items():
            if v is not None and (not isinstance(v, (int, float)) or v < 0):
                errors.append(f"{p['name']!r}: invalid value for {k}: {v!r}")
    with_value = sum(1 for p in pets if p.get("listed_value") is not None)
    if with_value / len(pets) < config.MIN_VALUE_COVERAGE:
        errors.append(
            f"only {with_value}/{len(pets)} pets have a numeric value "
            f"(< {config.MIN_VALUE_COVERAGE:.0%}); page structure may have changed"
        )
    return errors


# --------------------------------------------------------------------------- #
# Diagnostics: `python scraper.py --inspect`
# --------------------------------------------------------------------------- #
_JSON_HINTS = ("__NEXT_DATA__", "__NUXT__", "self.__next_f", "window.__", "application/json")


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
        out.append(card.prettify()[:3000])
        links = [a.get("href") for a in card.find_all("a")]
        out.append(f"links inside card: {links}")
    if not cards:
        out.append(
            "No cards matched. The page may be JS-rendered, blocked, or restructured.\n"
            "First 1500 chars of visible text:\n" + page_text[:1500]
        )
    return "\n".join(out)
