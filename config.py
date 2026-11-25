"""Central configuration for the AMVGG pet-demand scraper.

Everything tunable lives here so the other modules stay free of magic numbers.
"""
from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent
# AMVGG_DATA_DIR lets the tests (or you) write somewhere other than ./data
DATA_DIR = Path(os.environ.get("AMVGG_DATA_DIR", ROOT / "data"))
PETS_FILE = DATA_DIR / "pets.json"
HISTORY_DIR = DATA_DIR / "history"
INSPECT_REPORT = ROOT / "inspect-report.txt"

# --------------------------------------------------------------------------- #
# Source
# --------------------------------------------------------------------------- #
SOURCE_NAME = "AMVGG"
SOURCE_URL = os.environ.get("AMVGG_URL", "https://amvgg.com/values/pets")

# A descriptive, honest User-Agent. Override with AMVGG_USER_AGENT if needed.
USER_AGENT = os.environ.get(
    "AMVGG_USER_AGENT",
    "amvgg-pet-scraper/1.0 (personal project; GitHub Actions; 1 request per run)",
)

# --------------------------------------------------------------------------- #
# HTTP behaviour
# --------------------------------------------------------------------------- #
CONNECT_TIMEOUT = 10          # seconds
READ_TIMEOUT = 30             # seconds
MAX_RETRIES = 4               # total attempts per URL
BACKOFF_BASE = 2.0            # delay = BACKOFF_BASE * 2**(attempt-1) (+ jitter)
BACKOFF_MAX = 60.0            # cap on any single wait
MIN_REQUEST_INTERVAL = 2.0    # minimum seconds between any two requests
RETRY_STATUS = {429, 500, 502, 503, 504}

# --------------------------------------------------------------------------- #
# Validation / completeness
# --------------------------------------------------------------------------- #
MIN_PETS_ABSOLUTE = 100       # fewer than this is treated as a failed scrape
MAX_DROP_RATIO = 0.20         # reject if count falls >20% vs the previous run
MIN_DEMAND_COVERAGE = 0.98    # at least this share of pets must have a demand rating
MAX_CARD_TEXT = 400           # a single pet card's visible text is far shorter
MAX_NAME_LEN = 100
