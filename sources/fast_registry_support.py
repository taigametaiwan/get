from __future__ import annotations

import json
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_LOCK = threading.RLock()

GENERIC_IDENTITIES = {
    "", "LIVE", "TRUC TIEP", "XEM", "XEM NGAY", "LINK", "SERVER", "KENH",
    "DEFAULT", "UNKNOWN", "KHONG RO", "KHONG RO BLV", "BLV", "COMMENTATOR",
    "XOILAC", "XOI LAC", "XOILAC TV", "XOI LAC TV", "COLATV", "COLA TV",
    "LUONG SON", "LUONG SON TV", "CHUOI CHIEN", "CHUOI CHIEN TV",
    "PHAO HOA", "PHAO HOA TV", "GA VANG", "GA VANG TV", "S8", "S8 TV",
}


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_key(value: Any) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value).upper().replace("Đ", "D"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^A-Z0-9]+", " ", text).strip()


def identity_is_specific(value: Any, extra_generic: Iterable[str] = ()) -> bool:
    key = normalize_key(value)
    generic = GENERIC_IDENTITIES | {normalize_key(item) for item in extra_generic}
    if not key or key in generic:
        return False
    if re.fullmatch(r"KENH\s*\d+", key):
        return False
    if re.fullmatch(r"LINK\s*\d+", key):
        return False
    return len(key) >= 2


def load_registry(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_registry(path: Path, payload: dict[str, Any]) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)


def get_row(path: Path, identity: Any) -> dict[str, Any]:
    key = normalize_key(identity)
    row = (load_registry(path).get("commentators") or {}).get(key, {})
    return dict(row) if isinstance(row, dict) else {}


def set_row(path: Path, identity: Any, updates: dict[str, Any], source: str) -> bool:
    if not identity_is_specific(identity):
        return False
    key = normalize_key(identity)
    with _LOCK:
        payload = load_registry(path)
        payload.setdefault("schema_version", 1)
        rows = payload.setdefault("commentators", {})
        current = dict(rows.get(key) or {}) if isinstance(rows.get(key), dict) else {}
        comparable_current = {name: value for name, value in current.items() if name != "updated_at"}
        comparable_next = {**comparable_current, **updates, "source": source}
        if comparable_next == comparable_current:
            return False
        rows[key] = {**comparable_next, "updated_at": datetime.now(VN_TZ).isoformat()}
        save_registry(path, payload)
    return True


def url_expiry_epoch(value: str) -> int | None:
    try:
        query = parse_qs(urlparse(value).query)
    except Exception:
        return None
    for key in ("expire", "expires", "wsABSTime", "e"):
        raw = clean_text((query.get(key) or [""])[0])
        if raw.isdigit():
            return int(raw)
    return None


def url_is_usable_by_expiry(value: str, now_epoch: int, safety_seconds: int = 60) -> bool:
    expiry = url_expiry_epoch(value)
    return expiry is None or expiry > now_epoch + safety_seconds
