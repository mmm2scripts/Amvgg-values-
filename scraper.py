#!/usr/bin/env python3
"""AMVGG pet-demand scraper (pet name + demand only).

    python scraper.py              scrape, diff, write pets.json + history
    python scraper.py --dry-run    do everything except write files
    python scraper.py --history    show recorded history (no network)
    python scraper.py --check      health check: fetch + parse + validate, write nothing
    python scraper.py --inspect    dump the page's real markup to inspect-report.txt

Exit code 0 = success (including "nothing changed"); 1 = failure. On any
failure the existing data files are left untouched.
"""
from __future__ import annotations

import argparse
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

import config
import history
import parser as page_parser
import storage

log = logging.getLogger("amvgg")


class FetchError(Exception):
    pass


class BlockedError(FetchError):
    pass


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Fetcher:
    """requests.Session with timeouts, retries, exponential backoff, throttling."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5,*/*;q=0.3",
                "Accept-Language": "en",
            }
        )
        self._last_request = 0.0

    def _throttle(self) -> None:
        wait = config.MIN_REQUEST_INTERVAL - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), config.BACKOFF_MAX)
        delay = config.BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 1)
        return min(delay, config.BACKOFF_MAX)

    def get(self, url: str, ok_statuses: tuple[int, ...] = ()) -> requests.Response:
        last_problem = "unknown error"
        for attempt in range(1, config.MAX_RETRIES + 1):
            self._throttle()
            retry_after = None
            try:
                resp = self.session.get(url, timeout=(config.CONNECT_TIMEOUT, config.READ_TIMEOUT))
            except requests.RequestException as exc:
                last_problem = f"{type(exc).__name__}: {exc}"
                log.warning("Attempt %d/%d for %s failed: %s", attempt, config.MAX_RETRIES, url, last_problem)
            else:
                if resp.status_code == 200 or resp.status_code in ok_statuses:
                    return resp
                if resp.status_code in config.RETRY_STATUS:
                    last_problem = f"HTTP {resp.status_code}"
                    retry_after = resp.headers.get("Retry-After")
                    log.warning("Attempt %d/%d for %s: %s", attempt, config.MAX_RETRIES, url, last_problem)
                elif resp.status_code in (401, 403):
                    raise BlockedError(
                        f"HTTP {resp.status_code} from {url}. The site is refusing this client "
                        "(possibly blocking GitHub runner IPs or bots). Not attempting to bypass."
                    )
                else:
                    raise FetchError(f"HTTP {resp.status_code} from {url}")
            if attempt < config.MAX_RETRIES:
                delay = self._backoff(attempt, retry_after)
                log.info("Backing off %.1fs before retry", delay)
                time.sleep(delay)
        raise FetchError(f"Gave up on {url} after {config.MAX_RETRIES} attempts ({last_problem})")


def robots_allows(fetcher: Fetcher, url: str) -> bool:
    parts = urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    resp = fetcher.get(robots_url, ok_statuses=(404, 410))
    if resp.status_code in (404, 410):
        log.info("No robots.txt (HTTP %d); treating as allowed", resp.status_code)
        return True
    rp = RobotFileParser()
    rp.parse(resp.text.splitlines())
    allowed = rp.can_fetch(config.USER_AGENT, url)
    log.info("robots.txt %s fetching %s", "allows" if allowed else "DISALLOWS", parts.path)
    return allowed


def decode(resp: requests.Response) -> str:
    ctype = resp.headers.get("Content-Type", "").lower()
    encoding = resp.encoding if "charset" in ctype and resp.encoding else "utf-8"
    return resp.content.decode(encoding, errors="replace")


def get_html(args: argparse.Namespace) -> str:
    if args.html_file:
        log.info("Reading HTML from local file %s (no network)", args.html_file)
        with open(args.html_file, encoding="utf-8") as fh:
            return fh.read()
    fetcher = Fetcher()
    if not robots_allows(fetcher, config.SOURCE_URL):
        raise FetchError("robots.txt disallows this URL for our user agent; aborting")
    log.info("Fetching %s", config.SOURCE_URL)
    resp = fetcher.get(config.SOURCE_URL)
    html = decode(resp)
    log.info("Fetched %d bytes (HTTP %d)", len(resp.content), resp.status_code)
    return html


# --------------------------------------------------------------------------- #
# Validation (cross-run completeness checks)
# --------------------------------------------------------------------------- #
_CHALLENGE_RE = re.compile(r"just a moment|cf-challenge|captcha|attention required", re.I)


def validate(result: page_parser.ParseResult, previous: dict | None, force: bool,
             html: str = "") -> list[str]:
    errors: list[str] = []
    n = len(result.pets)

    if n == 0 and _CHALLENGE_RE.search(html):
        errors.append("page looks like an anti-bot/CAPTCHA challenge; not bypassing it")
    if n < config.MIN_PETS_ABSOLUTE:
        errors.append(f"only {n} pets parsed (minimum {config.MIN_PETS_ABSOLUTE})")

    if result.declared_total is None:
        log.warning("Could not find the 'Showing all N pets' marker; completeness vs the "
                    "page's own total is NOT verified this run")
    elif n < result.declared_total:
        errors.append(f"incomplete scrape: parsed {n} pets but page declares {result.declared_total}")
    elif n > result.declared_total:
        log.warning("Parsed %d pets but page declares %d", n, result.declared_total)

    prev_n = previous["total_pets"] if previous else 0
    if prev_n and n < prev_n * (1 - config.MAX_DROP_RATIO) and not force:
        errors.append(f"pet count dropped {prev_n} -> {n} (> {config.MAX_DROP_RATIO:.0%}); "
                      "use --force if this is legitimate")

    errors.extend(page_parser.validate_records(result.pets))
    return errors


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def parse_and_validate(args: argparse.Namespace, previous: dict | None):
    html = get_html(args)
    result = page_parser.parse_page(html, config.SOURCE_URL)
    for w in result.warnings[:20]:
        log.warning(w)
    if len(result.warnings) > 20:
        log.warning("... and %d more parser warnings", len(result.warnings) - 20)
    errors = validate(result, previous, args.force, html)
    return result, errors


def cmd_scrape(args: argparse.Namespace) -> int:
    previous = storage.load_pets()
    result, errors = parse_and_validate(args, previous)
    if errors:
        for e in errors:
            log.error("Validation failed: %s", e)
        log.error("Aborting. Existing data files were NOT modified.")
        return 1

    log.info("Parsed %d pets (page declares %s)", len(result.pets), result.declared_total)
    prev_pets = previous["pets"] if previous else []
    diff = history.diff_pets(prev_pets, result.pets)
    new_hash = storage.content_hash(result.pets)
    unchanged = previous is not None and storage.content_hash(prev_pets) == new_hash

    if unchanged:
        log.info("No changes since last run; nothing written")
        return 0

    for line in history.format_diff(diff, limit=40 if args.dry_run else 15):
        log.info(line)

    if args.dry_run:
        log.info("[dry-run] would write %s and a history snapshot; nothing written", config.PETS_FILE)
        return 0

    now = datetime.now(timezone.utc)
    payload = storage.build_payload(result.pets, now)
    # Snapshot first: if the process dies between the two writes, the next run
    # still sees a change and repairs pets.json (the snapshot dedupes itself).
    history.write_snapshot(config.HISTORY_DIR, payload, previous, diff, now)
    storage.save_payload(payload)
    log.info("Wrote %s (%d pets)", config.PETS_FILE, payload["total_pets"])
    write_step_summary(diff, payload)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    previous = storage.load_pets()
    result, errors = parse_and_validate(args, previous)
    with_demand = sum(1 for p in result.pets if p["demand_stars"] is not None)
    log.info("Parsed %d pets; page declares %s; %d with a demand rating",
             len(result.pets), result.declared_total, with_demand)
    if previous:
        log.info("Stored data has %d pets (last updated %s)",
                 previous["total_pets"], previous.get("last_updated"))
    if errors:
        for e in errors:
            log.error("CHECK FAILED: %s", e)
        return 1
    log.info("CHECK OK: site reachable, structure parses, scrape is complete")
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    html = get_html(args)
    report = page_parser.inspect_html(html, config.SOURCE_URL)
    config.INSPECT_REPORT.write_text(report + "\n", encoding="utf-8")
    print(report)
    log.info("Inspection report written to %s", config.INSPECT_REPORT)
    return 0


def write_step_summary(diff: dict, payload: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [f"### AMVGG pets: {payload['total_pets']} pets", "", "```"]
    lines += history.format_diff(diff, limit=30)
    lines.append("```")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Scrape Adopt Me pet demand from amvgg.com")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="scrape and diff but write nothing")
    mode.add_argument("--history", action="store_true", help="print recorded history and exit")
    mode.add_argument("--check", action="store_true", help="health check; write nothing")
    mode.add_argument("--inspect", action="store_true", help="write a report on the page's real markup")
    ap.add_argument("--pet", help="with --history: show the timeline for one pet")
    ap.add_argument("--limit", type=int, default=10, help="with --history: snapshots to list")
    ap.add_argument("--html-file", help="parse a saved HTML file instead of fetching (offline/debug)")
    ap.add_argument("--force", action="store_true", help="skip the 'pet count dropped' safety check")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
        stream=sys.stdout,
    )
    logging.Formatter.converter = time.gmtime

    if args.history:
        print(history.format_history(config.HISTORY_DIR, limit=args.limit, pet=args.pet))
        return 0
    try:
        if args.inspect:
            return cmd_inspect(args)
        if args.check:
            return cmd_check(args)
        return cmd_scrape(args)
    except FetchError as exc:
        log.error("%s", exc)
        log.error("Existing data files were NOT modified.")
        return 1
    except Exception:  # noqa: BLE001 - last-resort guard; data files untouched
        log.exception("Unexpected error. Existing data files were NOT modified.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
