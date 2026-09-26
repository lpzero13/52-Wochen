from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_local_env() -> None:
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, value = item.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


_load_local_env()

DEFAULT_NORGATE_PATH = PROJECT_ROOT / "data" / "norgate.sqlite"
NORGATE_DB_PATH = Path(os.environ.get("NORGATE_DB_PATH", DEFAULT_NORGATE_PATH)).expanduser()
SCANNER_DB_PATH = Path(os.environ.get("SCANNER_DB_PATH", "var/52w_scanner.sqlite"))
if not SCANNER_DB_PATH.is_absolute():
    SCANNER_DB_PATH = (PROJECT_ROOT / SCANNER_DB_PATH).resolve()
WEB_ROOT = PROJECT_ROOT / "web"
PORT = int(os.environ.get("PORT", "8020"))
