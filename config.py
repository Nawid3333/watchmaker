"""
Configuration for the watchmaker multi-domain batch watch-marker.
Loads credentials from watchmaker/config/.env and defines supported domains.
"""

import os
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv

# Resolve the project root once — used for .env loading, data/logs dirs, and
# resolving relative paths in DEFAULT_BATCH_FILE_PATH / SERIES_URLS_EXPORTS.
PROJECT_ROOT = os.path.abspath(os.path.dirname(__file__))
# Load environment variables from .env next to this config file (project root)
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))


# ==================== SUPPORTED DOMAINS ====================
# Map each exact host to its site family and credential key.
SUPPORTED_DOMAINS: dict[str, str] = {
    "aniworld.to": "aniworld",
    "aniworld.cc": "aniworld",
    "186.2.175.111": "aniworld",
    "bs.to": "bs",
    "bs.cine.to": "bs",
    "burningseries.ac": "bs",
    "burningseries.cx": "bs",
    "s.to": "sto",
    "serienstream.to": "sto",
    "186.2.175.5": "sto",
}

# Deterministic domain processing order
DOMAIN_ORDER = [
    "aniworld.to",
    "aniworld.cc",
    "186.2.175.111",
    "bs.to",
    "bs.cine.to",
    "burningseries.ac",
    "burningseries.cx",
    "s.to",
    "serienstream.to",
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
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0"
)


# ==================== USER CONFIG ====================
# Change this if you want the default batch file to live elsewhere.
# It can be an absolute path or a path relative to the project root.
DEFAULT_BATCH_FILE_PATH = "series_urls.txt"


# ==================== EXPORT TO SCRAPER LISTS ====================
# Map each site family to an external series_urls.txt file.
# After a successful run, watchmaker will append any newly seen URLs
# to these files. Set a value to None to disable exporting for that family.
# Relative paths are resolved against PROJECT_ROOT.
SERIES_URLS_EXPORTS: dict[str, str | None] = {
    "aniworld": r"v:\Coding projects\Aniworld.to HTTPX scraper\series_urls.txt",
    "bs": r"v:\Coding projects\BS.to HTTPX scraper\series_urls.txt",
    "sto": r"v:\Coding projects\S.to HTTPX scraper\series_urls.txt",
}


# ==================== STATE FILES ====================
FAILED_URLS_FILE = os.path.join(DATA_DIR, ".failed_urls.json")
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


SERIES_URLS_EXPORTS = {
    family: _resolve_export_path(path)
    for family, path in SERIES_URLS_EXPORTS.items()
}


def get_family(host: str) -> str | None:
    """Return the site family for a supported host, or None."""
    return SUPPORTED_DOMAINS.get(host.lower())


def credentials_for_family(family: str) -> dict:
    """Return the credentials dict for a site family."""
    return CREDENTIALS.get(family, {})


def validate_all_credentials() -> dict[str, bool]:
    """Check whether each configured family has non-empty credentials."""
    result: dict[str, bool] = {}
    for family, creds in CREDENTIALS.items():
        has_any = any(creds.values())
        result[family] = has_any
    return result
