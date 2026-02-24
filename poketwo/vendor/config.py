from __future__ import annotations

import os
from pathlib import Path

# ---------------------------
# Core toggles
# ---------------------------

DEBUG = os.getenv("POKETWO_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}

# ---------------------------
# Mongo
# ---------------------------

DATABASE_URI = os.getenv("POKETWO_DATABASE_URI", "")
DATABASE_NAME = os.getenv("POKETWO_DATABASE_NAME", "poketwo")

# ---------------------------
# Redis
# ---------------------------

REDIS_URL = os.getenv("REDIS_URL", "")

# Back-compat config dict (used by some legacy code paths; redis.py prefers REDIS_URL).
REDIS_CONF = {
    "host": os.getenv("POKETWO_REDIS_HOST", "localhost"),
    "port": int(os.getenv("POKETWO_REDIS_PORT", "6379")),
    "db": int(os.getenv("POKETWO_REDIS_DB", "0")),
    "password": os.getenv("POKETWO_REDIS_PASSWORD", "") or None,
}

# ---------------------------
# Assets / localization
# ---------------------------

# If you have a CDN or web server hosting images, set this.
ASSETS_BASE_URL = os.getenv("POKETWO_ASSETS_BASE_URL", "") or None

# Optional image rendering server from upstream deployments.
SERVER_URL = os.getenv("POKETWO_SERVER_URL", "") or None
EXT_SERVER_URL = os.getenv("POKETWO_EXT_SERVER_URL", "") or None

# Fluent language root inside the vendored poketwo tree.
LANG_ROOT = str((Path(__file__).parent / "poketwo" / "lang").resolve())

# Top.gg token (optional)
DBL_TOKEN = os.getenv("POKETWO_DBL_TOKEN")
