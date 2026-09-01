"""
Configuration for the watchmaker multi-domain batch watch-marker.
Loads credentials from watchmaker/config/.env and defines supported domains.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Resolve the project root once — used for .env loading, data/logs dirs, and
# resolving relative paths in DEFAULT_BATCH_FILE_PATH / SERIES_URLS_EXPORTS.
#
# Unset, WATCHMAKER_HOME leaves this as the repo checkout exactly as it always
# was, so running from a clone is byte-for-byte unchanged. Setting it is what
# makes an installed copy usable: in a venv this file sits inside
# site-packages, where no user can reasonably find a .env to edit.
_DEFAULT_HOME = os.path.abspath(os.path.dirname(__file__))
PROJECT_ROOT = os.path.abspath(os.environ.get("WATCHMAKER_HOME") or _DEFAULT_HOME)
# Load environment variables from .env at the project home
ENV_FILE = os.path.join(PROJECT_ROOT, ".env")
load_dotenv(ENV_FILE)


# ==================== SUPPORTED DOMAINS ====================
# Map each exact host to its site family and credential key.
# Dead primaries (bs.to, s.to) are intentionally omitted; the next reachable
# mirror in DOMAIN_ORDER will be used automatically.
SUPPORTED_DOMAINS: dict[str, str] = {
    "aniworld.to": "aniworld",
    "aniworld.cc": "aniworld",
    "186.2.175.111": "aniworld",
    "bs.cine.to": "bs",
    "burningseries.ac": "bs",
    "burningseries.cx": "bs",
    "serienstream.to": "sto",
    "serienstream.cx": "sto",
    "186.2.175.5": "sto",
}

# Deterministic domain processing order (first reachable host wins per family)
DOMAIN_ORDER = [
    "aniworld.to",
    "aniworld.cc",
    "186.2.175.111",
    "burningseries.ac",
    "burningseries.cx",
    "bs.cine.to",
    "serienstream.to",
    "serienstream.cx",
    "186.2.175.5",
]


# ==================== CREDENTIALS ====================
CREDENTIALS = {
    "aniworld": {
        "email": os.getenv("ANIWORLD_EMAIL", ""),
        "password": os.getenv("ANIWORLD_PASSWORD", ""),
    },
    "bs": {
        "username": os.getenv("BS_USERNAME", ""),
        "password": os.getenv("BS_PASSWORD", ""),
    },
    "sto": {
        "email": os.getenv("STO_EMAIL", ""),
        "password": os.getenv("STO_PASSWORD", ""),
    },
}


# ==================== DIRECTORIES ====================
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")

Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(LOGS_DIR).mkdir(parents=True, exist_ok=True)


# ==================== LOGGING ====================
LOG_FILE = os.path.join(LOGS_DIR, "watchmaker.log")


# ==================== HTTP SETTINGS ====================
HTTP_REQUEST_TIMEOUT = 20.0
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"


# ==================== USER CONFIG ====================
# Change this if you want the default batch file to live elsewhere.
# It can be an absolute path or a path relative to the project root.
DEFAULT_BATCH_FILE_PATH = "series_urls.txt"


# ==================== EXPORT TO SCRAPER LISTS ====================
# Map each site family to an external series_urls.txt file.
# These are the targets for menu option 3 (export) and the sources for
# option 4 (import); exporting is manual, never automatic after a run.
# Set a value to None to disable exporting for that family.
# Relative paths are resolved against PROJECT_ROOT.
#
# Defaults assume the scrapers sit next to this project, which is the normal
# layout, and are derived from that rather than hardcoded: an absolute path
# baked in here only works on the machine it was written on -- and publishes
# that machine's drive layout to anyone reading the repo.
# Override any of them with WATCHMAKER_<FAMILY>_URLS, or set to None to
# disable exporting for that family.
_SIBLING_ROOT = os.path.dirname(PROJECT_ROOT)
_DEFAULT_EXPORTS = {
    "aniworld": "Aniworld.to HTTPX scraper",
    "bs": "BS.to HTTPX scraper",
    "sto": "S.to HTTPX scraper",
}
SERIES_URLS_EXPORTS: dict[str, str | None] = {
    family: os.getenv(
        f"WATCHMAKER_{family.upper()}_URLS",
        os.path.join(_SIBLING_ROOT, folder, "series_urls.txt"),
    )
    for family, folder in _DEFAULT_EXPORTS.items()
}


# ==================== STATE FILES ====================
FAILED_URLS_FILE = os.path.join(DATA_DIR, ".failed_urls.json")
RETRY_BATCH_FILE = os.path.join(DATA_DIR, "retry_batch.txt")
DEFAULT_BATCH_FILE = (
    DEFAULT_BATCH_FILE_PATH
    if os.path.isabs(DEFAULT_BATCH_FILE_PATH)
    else os.path.join(PROJECT_ROOT, DEFAULT_BATCH_FILE_PATH)
)


def _resolve_export_path(path: str | None) -> str | None:
    """Return an absolute path for an export file, or None if disabled."""
    if not path:
        return None
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


SERIES_URLS_EXPORTS = {family: _resolve_export_path(path) for family, path in SERIES_URLS_EXPORTS.items()}
