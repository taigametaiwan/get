import asyncio
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
import time
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page, Route, async_playwright

try:
    from .hybrid_support import (
        extract_explicit_references,
        load_state as load_delta_state,
        save_state as save_delta_state,
        should_scan_now,
        update_state_from_results,
    )
except ImportError:  # chạy trực tiếp: python sources/<scanner>.py
    from hybrid_support import (
        extract_explicit_references,
        load_state as load_delta_state,
        save_state as save_delta_state,
        should_scan_now,
        update_state_from_results,
    )


# =========================
# CẤU HÌNH
# =========================
DEFAULT_HOME_URLS = (
    "https://smorf.io/",
)
TARGET_URL = DEFAULT_HOME_URLS[0]
PLAYER_ORIGIN_FALLBACK = "https://smorf.io"
GAVANG_STREAM_BASE = "https://flv.lauthaitv.cc/live/"
DEFAULT_GAVANG_SOURCE_LOGO_URL = "https://smorf.io/favicon.ico"
GAVANG_SOURCE_LOGO_URL = os.getenv(
    "GAVANG_SOURCE_LOGO_URL", DEFAULT_GAVANG_SOURCE_LOGO_URL
).strip()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_M3U = PROJECT_ROOT / "gavang_live.m3u"
OUTPUT_PIPE_M3U = PROJECT_ROOT / "gavang_live_pipe.m3u"
OUTPUT_VLC_M3U = PROJECT_ROOT / "gavang_live_vlc.m3u"
LEGACY_GIT_PLAYLIST_PATH = "gavang/gavang_live.m3u"
OUTPUT_DEBUG = "gavang_debug.json"
OUTPUT_HOME_DEBUG_HTML = "gavang_home_debug.html"
OUTPUT_HOME_DEBUG_PNG = "gavang_home_debug.png"
SCANNER_VERSION = "4.4.12-GAVANG-EXACT-FIXTURE-SCHEDULE-METADATA"


def read_env_bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    print(f"⚠️ {name}={raw!r} không hợp lệ; dùng mặc định {default}.")
    return default


def read_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"⚠️ {name}={raw!r} không hợp lệ; dùng mặc định {default}.")
        return default
    return max(minimum, min(value, maximum))


def read_env_urls(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    values = [part.strip() for part in raw.split(",") if part.strip()] if raw else list(defaults)
    normalized: list[str] = []
    for value in values:
        if not value.startswith(("http://", "https://")):
            continue
        fixed = value.rstrip("/") + "/"
        if fixed not in normalized:
            normalized.append(fixed)
    return tuple(normalized or defaults)


HOME_URLS = read_env_urls("GAVANG_HOME_URLS", DEFAULT_HOME_URLS)
TARGET_URL = HOME_URLS[0]


CONCURRENCY_LIMIT = read_env_int(
    "GAVANG_MATCH_CONCURRENCY", 4, minimum=1, maximum=12
)
HOME_WAIT_MS = read_env_int(
    "GAVANG_HOME_WAIT_MS", 6000, minimum=1000, maximum=30000
)
STREAM_WAIT_SECONDS = read_env_int(
    "GAVANG_ROOM_WAIT_SECONDS", 20, minimum=5, maximum=120
)
EXTRA_WAIT_AFTER_FIRST_STREAM = 5.0
FULL_SCAN = read_env_bool("GAVANG_FULL_SCAN", True)
VERIFY_STREAMS = read_env_bool("GAVANG_VERIFY_STREAMS", True)
VERIFY_TIMEOUT_SECONDS = read_env_int("GAVANG_VERIFY_TIMEOUT_SECONDS", 8, minimum=3, maximum=20)
MAX_VERIFY_CANDIDATES = read_env_int("GAVANG_MAX_VERIFY_CANDIDATES", 6, minimum=2, maximum=12)
MAX_OUTPUT_STREAMS_PER_MATCH = read_env_int("GAVANG_MAX_OUTPUT_STREAMS_PER_MATCH", 2, minimum=1, maximum=4)
UPCOMING_KEEP_HOURS = read_env_int("GAVANG_UPCOMING_KEEP_HOURS", 4, minimum=1, maximum=12)
SCAN_PAST_MINUTES = read_env_int("GAVANG_SCAN_PAST_MINUTES", 150, minimum=0, maximum=1440)
SCAN_FUTURE_MINUTES = read_env_int("GAVANG_SCAN_FUTURE_MINUTES", 240, minimum=0, maximum=1440)
SCAN_UNKNOWN_LIVE = read_env_bool("GAVANG_SCAN_UNKNOWN_LIVE", True)
PROBE_UNKNOWN_STREAM_KEYS = read_env_bool("GAVANG_PROBE_UNKNOWN_STREAM_KEYS", True)
KEEP_DERIVED_PENDING = read_env_bool("GAVANG_KEEP_DERIVED_PENDING", True)
KEEP_UNKNOWN_TIME_PENDING = read_env_bool("GAVANG_KEEP_UNKNOWN_TIME_PENDING", True)
UPCOMING_MIN_CANDIDATE_SCORE = read_env_int("GAVANG_UPCOMING_MIN_CANDIDATE_SCORE", 150, minimum=80, maximum=300)
ALLOW_UNVERIFIED_BROWSER_FALLBACK = read_env_bool("GAVANG_ALLOW_UNVERIFIED_BROWSER_FALLBACK", False)
KEEP_PREVIOUS_UNVERIFIED = read_env_bool("GAVANG_KEEP_PREVIOUS_UNVERIFIED", False)
UPCOMING_FAR_THRESHOLD_MINUTES = read_env_int("GAVANG_UPCOMING_FAR_THRESHOLD_MINUTES", 45, minimum=5, maximum=240)
UPCOMING_FAR_WAIT_SECONDS = read_env_int("GAVANG_UPCOMING_FAR_WAIT_SECONDS", 7, minimum=3, maximum=30)
UPCOMING_NEAR_WAIT_SECONDS = read_env_int("GAVANG_UPCOMING_NEAR_WAIT_SECONDS", 12, minimum=5, maximum=60)
HYBRID_HTTP_FIRST = read_env_bool("GAVANG_HYBRID_HTTP_FIRST", True)
HTTP_DISCOVERY_TIMEOUT_SECONDS = read_env_int("GAVANG_HTTP_DISCOVERY_TIMEOUT_SECONDS", 8, minimum=3, maximum=20)
HTTP_DISCOVERY_MAX_FOLLOWS = read_env_int("GAVANG_HTTP_DISCOVERY_MAX_FOLLOWS", 4, minimum=1, maximum=10)
DELTA_SCAN_ENABLED = read_env_bool("GAVANG_DELTA_SCAN_ENABLED", True)
DELTA_NEAR_MINUTES = read_env_int("GAVANG_DELTA_NEAR_MINUTES", 45, minimum=5, maximum=180)
STATE_PATH = Path(os.getenv("GAVANG_STATE_PATH", "gavang_state.json"))
HEADLESS = True
PROBE_CACHE: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}

# Dùng đúng User-Agent đã được kiểm chứng phát được bằng VLC.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

STREAM_EXTENSIONS = (".m3u8", ".flv")
AD_MARKERS = (
    "doubleclick.",
    "googleads.",
    "/ads/",
    "/advert",
    "imasdk",
)

PLAY_SELECTORS = (
    ".vjs-big-play-button",
    ".plyr__control--overlaid",
    ".jw-icon-display",
    ".jw-display-icon-container",
    ".play-button",
    ".btn-play",
    "button[aria-label*='Play' i]",
    "button[title*='Play' i]",
    "[class*='play'][role='button']",
)

TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:h.]([0-5]\d)(?!\d)", re.I)
DATE_DMY_RE = re.compile(r"(?<!\d)(0?[1-9]|[12]\d|3[01])[\-/\.](0?[1-9]|1[0-2])(?:[\-/\.](20\d{2}|\d{2}))?(?!\d)")

BLV_ALIASES = {
    "angao": "A Ngáo",
}

QUALITY_TEXT_RE = re.compile(
    r"(?i)\b(4k|uhd|2160p?|full\s*hd|fhd|1080p?|hd|720p?|sd|480p?|auto)\b"
)


SPORT_GROUP_ORDER = (
    "Bóng đá",
    "Bóng rổ",
    "Bóng chuyền",
    "Tennis",
    "Esports",
    "Khác",
)
SPORT_GROUP_RANK = {name: index for index, name in enumerate(SPORT_GROUP_ORDER)}
SPORT_KEYWORDS: dict[str, tuple[tuple[str, int], ...]] = {
    "Esports": (
        ("esports", 12), ("e sports", 12), ("esport", 12),
        ("counter strike", 9), ("cs2", 9), ("csgo", 9),
        ("dota", 9), ("league of legends", 9), ("valorant", 9),
        ("pubg", 8), ("mobile legends", 8), ("lien quan", 8),
        ("efootball", 8), ("fifa online", 8), ("arena of valor", 8),
    ),
    "Tennis": (
        ("tennis", 12), ("quan vot", 12), ("atp", 8), ("wta", 8),
        ("challenger", 7), ("wimbledon", 8), ("roland garros", 8),
        ("australian open", 8), ("us open", 7), ("davis cup", 7),
    ),
    "Bóng rổ": (
        ("bong ro", 12), ("basketball", 12), ("nba", 9), ("wnba", 9),
        ("euroleague", 8), ("fiba", 8), ("ncaa", 7), ("vba", 7),
        ("cba", 6), ("basket", 6),
    ),
    "Bóng chuyền": (
        ("bong chuyen", 12), ("volleyball", 12), ("fivb", 9),
        ("volleyball nations league", 10), ("nations league women", 8),
        ("nations league men", 8), ("vnl", 8), ("pvl", 7),
        ("cev", 5),
    ),
    "Bóng đá": (
        ("bong da", 12), ("football", 11), ("soccer", 11),
        ("futsal", 10), ("premier league", 8), ("champions league", 8),
        ("europa league", 8), ("conference league", 8),
        ("world cup", 7), ("asian cup", 7), ("copa", 6),
        ("uefa", 6), ("afc", 5), ("fc ", 4), (" fc", 4),
    ),
    "Khác": (
        ("cau long", 12), ("badminton", 12), ("bong ban", 12),
        ("table tennis", 12), ("baseball", 10), ("ice hockey", 10),
        ("hockey", 8), ("handball", 9), ("boxing", 9), ("mma", 9),
        ("motogp", 9), ("formula 1", 9), ("f1 racing", 9),
    ),
}


def normalize_search_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value).lower().replace("đ", "d"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return f" {clean_text(text)} "


def classify_sport(*values: str, default: str = "Bóng đá") -> str:
    """Phân loại theo tín hiệu gần card/trang trận; tín hiệu đầu tiên có độ tin cậy cao thắng."""
    for value in values:
        normalized = normalize_search_text(value)
        if not normalized.strip():
            continue
        scores: dict[str, int] = {}
        for group, keywords in SPORT_KEYWORDS.items():
            score = 0
            for keyword, weight in keywords:
                token = f" {keyword.strip()} "
                if token in normalized or (len(keyword.strip()) >= 6 and keyword.strip() in normalized):
                    score += weight
            if score:
                scores[group] = score
        if not scores:
            continue
        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        if len(ranked) == 1 or ranked[0][1] > ranked[1][1]:
            return ranked[0][0]
    return default if default in SPORT_GROUP_RANK else "Khác"


def channel_id_for(result: dict[str, Any], stream_url: str, index: int) -> str:
    base = match_id_from_url(result.get("url", "")) or hashlib.sha1(
        (result.get("url", "") + stream_url).encode("utf-8")
    ).hexdigest()[:12]
    return f"gavang-{base}-{index}"

def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _decode_javascript_escapes(value: str) -> str:
    text = value or ""
    text = re.sub(
        r"\\u([0-9a-fA-F]{4})",
        lambda match: chr(int(match.group(1), 16)),
        text,
    )
    text = re.sub(
        r"\\x([0-9a-fA-F]{2})",
        lambda match: chr(int(match.group(1), 16)),
        text,
    )
    return text.replace("\\/", "/")


def decode_url_repeatedly(value: str, rounds: int = 5) -> str:
    current = html.unescape(value or "").strip()
    for _ in range(rounds):
        decoded = html.unescape(_decode_javascript_escapes(current))
        decoded = unquote(decoded)
        if decoded == current:
            current = decoded
            break
        current = decoded
    return current.strip()


def normalize_blv_name(value: str) -> str:
    raw = clean_text(decode_url_repeatedly(value))
    raw = re.sub(r"(?i)^\s*(?:blv|bình\s*luận\s*viên)\s*[:\-–—]?\s*", "", raw)

    # DOM Gà Vàng thường trả cả nội dung của nút/chọn server, ví dụ:
    # "NGƯỜI CHÈ TRẬN Đổi trận Bình luận Mô phỏng ...". Chỉ giữ phần
    # tên đứng trước các nhãn điều khiển; nếu không cắt, M3U sẽ hiện cả câu rác.
    raw = re.split(
        r"(?i)\s+\b(?:trận|đổi\s+trận|bình\s+luận|mô\s+phỏng|server|"
        r"chất\s+lượng|xem\s+ngay|phát\s+trực\s+tiếp)\b",
        raw,
        maxsplit=1,
    )[0]
    raw = raw.strip(" -|•[]()")
    if not raw or len(raw) > 60 or re.search(r"(?i)\bvs\b", raw):
        return ""

    generic = normalize_search_text(raw).strip()
    if generic in {"binh luan", "binh luan vien", "doi tran", "mo phong", "khong ro"}:
        return ""

    key = generic.replace(" ", "")
    if key in BLV_ALIASES:
        return BLV_ALIASES[key]

    if re.fullmatch(r"[a-zA-Z0-9_.-]+", raw):
        words = re.sub(r"[_\-.]+", " ", raw).split()
        return " ".join(word.capitalize() for word in words)
    return raw


def extract_blv_from_url(value: str) -> str:
    try:
        query = parse_qs(urlparse(decode_url_repeatedly(value)).query)
    except Exception:
        return ""
    for key in ("blvName", "blv_name", "commentator", "commentatorName", "blv"):
        values = query.get(key) or query.get(key.lower())
        if values:
            name = normalize_blv_name(values[0])
            if name:
                return name
    return ""


def normalize_quality_hint(value: str) -> str:
    text = clean_text(decode_url_repeatedly(value))
    if not text:
        return ""
    match = QUALITY_TEXT_RE.search(text)
    if not match:
        return ""
    token = match.group(1).lower().replace(" ", "")
    if token in {"4k", "uhd", "2160", "2160p"}:
        return "4K"
    if token in {"fullhd", "fhd", "1080", "1080p"}:
        return "FHD"
    if token in {"hd", "720", "720p"}:
        return "HD"
    if token in {"sd", "480", "480p"}:
        return "SD"
    if token == "auto":
        return "AUTO"
    return token.upper()


def parse_hls_variants(text: str, base_url: str) -> list[dict[str, str]]:
    if "#EXTM3U" not in (text or "") or "#EXT-X-STREAM-INF" not in text:
        return []
    lines = [line.strip() for line in text.splitlines()]
    variants: list[dict[str, str]] = []
    pending = ""
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = line.partition(":")[2]
            continue
        if not pending or not line or line.startswith("#"):
            continue
        quality = normalize_quality_hint(pending)
        resolution = re.search(r"RESOLUTION=\d+x(\d+)", pending, re.I)
        if resolution:
            height = int(resolution.group(1))
            quality = "4K" if height >= 1800 else "FHD" if height >= 1000 else "HD" if height >= 700 else "SD"
        variants.append({
            "url": urljoin(base_url, line),
            "quality": quality,
            "parent_url": base_url,
        })
        pending = ""
    return variants


def absolute_url(value: str, base: str = TARGET_URL) -> str:
    value = decode_url_repeatedly(value)
    if not value or value.startswith(("data:", "blob:", "javascript:")):
        return ""
    try:
        return urljoin(base, value)
    except Exception:
        return value


LOGO_VALUE_KEYS = (
    "url", "contentUrl", "content_url", "src", "href", "@id", "value",
    "image", "logo",
)
INVALID_LOGO_TEXT = {
    "", "none", "null", "undefined", "[object object]", "object object",
    "[object promise]",
}


def extract_logo_scalar(value: Any, depth: int = 0) -> str:
    """Lấy URL ảnh thật từ string/list/object JSON-LD, không stringify object JS."""
    if depth > 6 or value is None:
        return ""
    if isinstance(value, dict):
        for key in LOGO_VALUE_KEYS:
            if key in value:
                found = extract_logo_scalar(value.get(key), depth + 1)
                if found:
                    return found
        return ""
    if isinstance(value, (list, tuple, set)):
        for item in value:
            found = extract_logo_scalar(item, depth + 1)
            if found:
                return found
        return ""
    raw = clean_text(str(value))
    if not raw or raw.lower() in INVALID_LOGO_TEXT or "[object object]" in raw.lower():
        return ""
    if raw.startswith(("{", "[")):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if parsed is not None:
            return extract_logo_scalar(parsed, depth + 1)
    return raw


def normalize_logo_url(value: Any, base: str = TARGET_URL) -> str:
    raw = extract_logo_scalar(value)
    if not raw or raw.startswith(("data:", "blob:", "javascript:")):
        return ""
    resolved = absolute_url(raw, base)
    if not resolved or "[object object]" in resolved.lower():
        return ""
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return resolved


def default_gavang_source_logo(base: str = TARGET_URL) -> str:
    configured = normalize_logo_url(GAVANG_SOURCE_LOGO_URL, base)
    if configured:
        return configured
    try:
        parsed = urlparse(base)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
    except Exception:
        pass
    return DEFAULT_GAVANG_SOURCE_LOGO_URL


def origin_from_url(value: str) -> str:
    try:
        parsed = urlparse(value)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    except Exception:
        pass
    return ""


def extract_datetime_parts(value: str) -> tuple[str, str]:
    """Trả về (giờ HH:MM, ngày DD/MM) từ đúng một candidate thời gian."""
    text = clean_text(value)
    if not text:
        return "", ""

    # ISO có timezone được đổi sang giờ Việt Nam. ISO không timezone được coi là giờ hiển thị của trang.
    iso_match = re.search(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?",
        text,
    )
    if iso_match:
        try:
            iso_value = iso_match.group(0).replace("Z", "+00:00")
            parsed = datetime.fromisoformat(iso_value)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(ZoneInfo("Asia/Ho_Chi_Minh"))
            return parsed.strftime("%H:%M"), parsed.strftime("%d/%m")
        except Exception:
            pass

    time_match = TIME_RE.search(text)
    time_str = f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else ""
    date_match = DATE_DMY_RE.search(text)
    date_str = ""
    if date_match:
        date_str = f"{int(date_match.group(1)):02d}/{int(date_match.group(2)):02d}"
    return time_str, date_str


def extract_time(value: str) -> str:
    return extract_datetime_parts(value)[0]


def extract_date(value: str) -> str:
    return extract_datetime_parts(value)[1]


def select_best_time_candidate(metadata: dict[str, Any]) -> tuple[str, str, str]:
    """Chọn giờ từ candidate có điểm cao nhất, không quét chuỗi thời gian toàn trang theo thứ tự ngẫu nhiên."""
    candidates = metadata.get("time_candidates") or []
    ranked: list[tuple[int, str, str, str]] = []
    for item in candidates:
        if isinstance(item, dict):
            value = clean_text(str(item.get("value", "")))
            score = int(item.get("score") or 0)
            source = clean_text(str(item.get("source", "")))
        else:
            value = clean_text(str(item))
            score = 0
            source = "legacy"
        time_str, date_str = extract_datetime_parts(value)
        if time_str:
            ranked.append((score, time_str, date_str, source))
    if ranked:
        ranked.sort(key=lambda row: row[0], reverse=True)
        _, time_str, date_str, source = ranked[0]
        return time_str, date_str, source
    legacy = clean_text(str(metadata.get("time_text", "")))
    time_str, date_str = extract_datetime_parts(legacy)
    return time_str, date_str, "legacy-time-text" if time_str else ""



VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
QUALITY_PRIORITY = {"4K": 50, "FHD": 40, "HD": 30, "SD": 20, "AUTO": 10, "": 0}
PLAYABILITY_PRIORITY = {
    "verified": 40,
    "upcoming-pending": 30,
    "browser-observed": 20,
    "previous-fallback": 10,
    "not-checked": 5,
}


def resolve_kickoff_datetime(
    time_str: str,
    date_str: str = "",
    now: datetime | None = None,
) -> tuple[datetime | None, str]:
    """Ghép giờ/ngày của trang thành datetime VN; ngày thiếu được suy luận gần thời điểm quét nhất."""
    time_match = re.fullmatch(r"\s*([01]?\d|2[0-3]):([0-5]\d)\s*", time_str or "")
    if not time_match:
        return None, "missing-time"
    now = now.astimezone(VN_TZ) if now and now.tzinfo else (now.replace(tzinfo=VN_TZ) if now else datetime.now(VN_TZ))
    hour = int(time_match.group(1))
    minute = int(time_match.group(2))

    date_match = re.fullmatch(r"\s*(0?[1-9]|[12]\d|3[01])/(0?[1-9]|1[0-2])\s*", date_str or "")
    candidates: list[datetime] = []
    if date_match:
        day = int(date_match.group(1))
        month = int(date_match.group(2))
        for year in (now.year - 1, now.year, now.year + 1):
            try:
                candidates.append(datetime(year, month, day, hour, minute, tzinfo=VN_TZ))
            except ValueError:
                pass
        source = "explicit-date"
    else:
        today = now.date()
        for offset in (-1, 0, 1):
            day = today + timedelta(days=offset)
            candidates.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=VN_TZ))
        source = "inferred-nearest-date"

    if not candidates:
        return None, "invalid-date"
    # Trang chủ thường chỉ hiển thị hôm nay/ngày mai. Chọn mốc gần thời điểm quét nhất
    # giúp 08:00 lúc 23:00 được hiểu là sáng hôm sau, còn 23:00 lúc 01:00 là tối hôm trước.
    kickoff = min(candidates, key=lambda item: abs((item - now).total_seconds()))
    return kickoff, source


def annotate_match_timing(match: dict[str, Any], now: datetime | None = None) -> None:
    now = now.astimezone(VN_TZ) if now and now.tzinfo else (now.replace(tzinfo=VN_TZ) if now else datetime.now(VN_TZ))
    kickoff, date_resolution = resolve_kickoff_datetime(
        clean_text(str(match.get("time", ""))),
        clean_text(str(match.get("date", ""))),
        now,
    )
    match["scan_time_iso"] = now.isoformat()
    match["kickoff_iso"] = kickoff.isoformat() if kickoff else ""
    match["kickoff_resolution"] = date_resolution
    match["minutes_to_kickoff"] = None
    match["timing_state"] = "unknown"
    match["upcoming_within_window"] = False
    if not kickoff:
        return

    delta_minutes = int(round((kickoff - now).total_seconds() / 60))
    match["minutes_to_kickoff"] = delta_minutes
    if not match.get("date") and abs(delta_minutes) <= 12 * 60:
        match["date"] = kickoff.strftime("%d/%m")

    window_minutes = UPCOMING_KEEP_HOURS * 60
    if 0 <= delta_minutes <= window_minutes:
        state = "upcoming-window"
        match["upcoming_within_window"] = True
    elif delta_minutes > window_minutes:
        state = "future"
    elif -180 <= delta_minutes < 0:
        state = "started-recently"
    else:
        state = "past"
    match["timing_state"] = state


def is_upcoming_within_window(match: dict[str, Any]) -> bool:
    delta = match.get("minutes_to_kickoff")
    return (
        match.get("timing_state") == "upcoming-window"
        and isinstance(delta, int)
        and 0 <= delta <= UPCOMING_KEEP_HOURS * 60
    )


def _has_explicit_live_hint(match: dict[str, Any]) -> bool:
    """Chỉ cứu trận thiếu giờ khi card nói rõ đang diễn ra; không coi chữ 'trực tiếp' chung là LIVE."""
    raw = " ".join(
        clean_text(str(match.get(key, "")))
        for key in ("card_text", "sport_hint", "raw_title", "status_text")
    )
    normalized = normalize_search_text(raw)
    markers = (
        " dang dien ra ", " dang da ", " live now ", " currently live ",
        " in play ", " hiep 1 ", " hiep 2 ", " halftime ",
    )
    return any(marker in normalized for marker in markers)


def filter_links_by_scan_window(
    links: list[dict[str, Any]],
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Lọc TRƯỚC khi mở trang trận: đã bắt đầu <=120 phút hoặc sắp đá <=240 phút."""
    now = now.astimezone(VN_TZ) if now and now.tzinfo else (
        now.replace(tzinfo=VN_TZ) if now else datetime.now(VN_TZ)
    )
    kept: list[dict[str, Any]] = []
    stats = {
        "total": len(links), "window": 0, "unknown_live": 0,
        "unknown_key_probe": 0, "past": 0, "future": 0, "unknown": 0,
    }
    for item in links:
        _name, derived_time, _blv = derive_match_info(
            str(item.get("url", "")),
            str(item.get("raw_title", "")),
            str(item.get("raw_time", "")),
        )
        if not item.get("time"):
            item["time"] = derived_time
        if not item.get("date"):
            item["date"] = (
                extract_date(str(item.get("raw_time", "")))
                or extract_date(str(item.get("raw_title", "")))
                or extract_date(str(item.get("card_text", "")))
            )
        annotate_match_timing(item, now)
        delta = item.get("minutes_to_kickoff")
        if isinstance(delta, int):
            if -SCAN_PAST_MINUTES <= delta <= SCAN_FUTURE_MINUTES:
                item["scan_window_reason"] = "time-window"
                kept.append(item)
                stats["window"] += 1
            elif delta < -SCAN_PAST_MINUTES:
                item["scan_window_reason"] = "too-old"
                stats["past"] += 1
            else:
                item["scan_window_reason"] = "too-early"
                stats["future"] += 1
            continue

        if SCAN_UNKNOWN_LIVE and _has_explicit_live_hint(item):
            item["scan_window_reason"] = "unknown-time-live"
            kept.append(item)
            stats["unknown_live"] += 1
        elif PROBE_UNKNOWN_STREAM_KEYS and extract_gavang_stream_key(str(item.get("url", ""))):
            # Trang chủ Gà Vàng thường không gắn giờ hoặc nhãn LIVE cho mọi trận đang phát.
            # Với URL /s8-live/<fixture>/<stream_key>/ ta vẫn có thể probe trực tiếp FLV
            # mà không cần mở Chromium. Giữ các URL này ở chế độ probe-only để không bỏ
            # sót trận như fixture 2448 dalian-beijing-chnfa.
            item["scan_window_reason"] = "unknown-time-derived-probe"
            item["derived_probe_only"] = True
            kept.append(item)
            stats["unknown_key_probe"] += 1
        else:
            item["scan_window_reason"] = "unknown-time"
            stats["unknown"] += 1
    return kept, stats


def print_scan_window_summary(stats: dict[str, int]) -> None:
    print(
        "🕒 Lọc cửa sổ quét "
        f"[-{SCAN_PAST_MINUTES}, +{SCAN_FUTURE_MINUTES}] phút: "
        f"tổng={stats.get('total', 0)} | giữ={stats.get('window', 0) + stats.get('unknown_live', 0) + stats.get('unknown_key_probe', 0)} "
        f"(đúng giờ={stats.get('window', 0)}, LIVE thiếu giờ={stats.get('unknown_live', 0)}, "
        f"probe khóa FLV={stats.get('unknown_key_probe', 0)}) | "
        f"loại quá cũ={stats.get('past', 0)} | quá sớm={stats.get('future', 0)} | "
        f"không rõ giờ={stats.get('unknown', 0)}",
        flush=True,
    )



def effective_stream_wait_seconds(match: dict[str, Any]) -> int:
    """Rút ngắn phiên cho trận còn xa; trận gần giờ/live vẫn quét đủ."""
    delta = match.get("minutes_to_kickoff")
    if isinstance(delta, int) and delta > 0:
        if delta > UPCOMING_FAR_THRESHOLD_MINUTES:
            return min(STREAM_WAIT_SECONDS, UPCOMING_FAR_WAIT_SECONDS)
        return min(STREAM_WAIT_SECONDS, UPCOMING_NEAR_WAIT_SECONDS)
    return STREAM_WAIT_SECONDS


def should_probe_quality_buttons(match: dict[str, Any], has_candidate: bool = False) -> bool:
    delta = match.get("minutes_to_kickoff")
    if has_candidate:
        return True
    if not isinstance(delta, int):
        return True
    return delta <= UPCOMING_FAR_THRESHOLD_MINUTES

def quality_rank(value: str) -> int:
    return QUALITY_PRIORITY.get(normalize_quality_hint(value), 0)


def apply_paired_quality_hints(entries: list[dict[str, Any]]) -> None:
    """Chuẩn hóa cặp tên kiểu angao/angaohd thành HD/FHD và ưu tiên metadata HLS thật."""
    families_with_hd: set[str] = set()
    for entry in entries:
        channel = stream_channel_key(entry.get("url", ""))
        family = stream_family_key(entry.get("url", ""))
        if family and channel != family and re.search(r"(?:fullhd|fhd|1080p?|hd|720p?)$", channel, re.I):
            families_with_hd.add(family)

    for entry in entries:
        channel = stream_channel_key(entry.get("url", ""))
        family = stream_family_key(entry.get("url", ""))
        explicit = normalize_quality_hint(entry.get("quality", ""))
        from_variant = bool(entry.get("parent_url"))
        if re.search(r"(?:fullhd|fhd|1080p?|hd)$", channel, re.I):
            inferred = "FHD"
        elif family and family in families_with_hd and channel == family:
            inferred = "HD"
        else:
            inferred = explicit
        # RESOLUTION trong master HLS đáng tin hơn tên channel; click UI đơn thuần thì không.
        if from_variant and explicit:
            inferred = explicit
        entry["quality"] = inferred
        entry["quality_rank"] = quality_rank(inferred)


def select_best_quality_streams(
    streams: list[dict[str, Any]],
    limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Chỉ giữ một URL tốt nhất cho mỗi mức chất lượng; ưu tiên M3U8 và stream đã xác minh."""
    apply_paired_quality_hints(streams)
    ranked = sorted(
        streams,
        key=lambda item: (
            PLAYABILITY_PRIORITY.get(str(item.get("playability", "")), 0),
            int(item.get("quality_rank") or quality_rank(item.get("quality", ""))),
            2 if stream_kind(item.get("url", ""), item.get("content_type", "")) == "m3u8" else 1,
            int(item.get("candidate_score") or 0),
            item.get("url", ""),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    used_tiers: set[str] = set()
    used_urls: set[str] = set()

    for entry in ranked:
        url = canonicalize_stream_url(entry.get("url", ""))
        if not url or url in used_urls:
            entry["reject_reason"] = "trùng URL"
            rejected.append(entry)
            continue
        quality = normalize_quality_hint(entry.get("quality", ""))
        tier = quality or "UNKNOWN"
        if tier in used_tiers:
            entry["reject_reason"] = (
                f"trùng mức chất lượng {tier}; đã ưu tiên stream có độ tin cậy/khả năng tương thích cao hơn"
            )
            rejected.append(entry)
            continue
        if len(selected) >= limit:
            entry["reject_reason"] = f"vượt giới hạn {limit} mức chất lượng tốt nhất"
            rejected.append(entry)
            continue
        entry["url"] = url
        selected.append(entry)
        used_urls.add(url)
        used_tiers.add(tier)

    return selected, rejected


def stream_kind(url: str, content_type: str = "") -> str:
    clean = decode_url_repeatedly(url)
    lower_path = urlparse(clean).path.lower()
    lower_type = (content_type or "").lower()

    if ".m3u8" in lower_path or any(marker in lower_type for marker in (
        "application/vnd.apple.mpegurl", "application/x-mpegurl",
        "audio/mpegurl", "audio/x-mpegurl",
    )):
        return "m3u8"
    if ".flv" in lower_path or any(marker in lower_type for marker in (
        "video/x-flv", "video/flv", "application/x-flv",
    )):
        return "flv"
    return ""


WRAPPER_QUERY_KEYS = {"autoplay", "ishome", "is_home", "muted", "controls"}


def canonicalize_stream_url(value: str) -> str:
    """Làm sạch URL media nhưng giữ nguyên query token/chữ ký hợp lệ."""
    clean = decode_url_repeatedly(value).strip().rstrip("),];'\"")
    if not clean:
        return ""
    match = re.match(
        r"(?is)^(https?://.*?\.(?:m3u8|flv))(?P<tail>[?&#].*)?$",
        clean,
    )
    if not match:
        return clean
    base = match.group(1)
    tail = match.group("tail") or ""
    if tail.startswith("&"):
        # Đây là tham số của URL embed bị nối nhầm sau streamUrl.
        return base
    if tail.startswith("#"):
        return base
    if tail.startswith("?"):
        raw_parts = [part for part in tail[1:].split("&") if part]
        kept = []
        for part in raw_parts:
            key = part.split("=", 1)[0].strip().lower()
            if key in WRAPPER_QUERY_KEYS:
                continue
            kept.append(part)
        return base + ("?" + "&".join(kept) if kept else "")
    return base


def stream_channel_key(url: str) -> str:
    """Ví dụ /live/angao/playlist.m3u8 -> angao; /live/chuoichao.flv -> chuoichao."""
    path = urlparse(canonicalize_stream_url(url)).path.strip("/")
    if not path:
        return ""
    parts = [part for part in path.split("/") if part]
    last = parts[-1].lower()
    if last in {"playlist.m3u8", "index.m3u8", "master.m3u8"} and len(parts) >= 2:
        return re.sub(r"[^a-z0-9_-]+", "", parts[-2].lower())
    stem = re.sub(r"\.(?:m3u8|flv)$", "", last, flags=re.I)
    return re.sub(r"[^a-z0-9_-]+", "", stem.lower())


def stream_family_key(url: str) -> str:
    key = stream_channel_key(url)
    return re.sub(r"(?:[-_]?)(?:fullhd|fhd|1080p?|hd|720p?)$", "", key, flags=re.I)


def _entry_is_browser_observed(entry: dict[str, Any]) -> bool:
    for source in entry.get("sources") or []:
        if (
            source.startswith("request/")
            or source.startswith("http/")
            or source == "response"
            or source == "iframe/src"
            or source == "hls/variant"
            or source == "home-card/stream-hint"
            or source.startswith("dom/")
            or source.startswith("quality/")
        ):
            return True
    return False


def _entry_is_high_confidence_observed(entry: dict[str, Any]) -> bool:
    """Nguồn đủ chắc để fallback khi runner bị CDN chặn."""
    for source in entry.get("sources") or []:
        if (
            source.startswith("request/")
            or source.startswith("http/")
            or source == "response"
            or source == "iframe/src"
            or source == "hls/variant"
            or source == "home-card/stream-hint"
            or source == "metadata/quality-source"
        ):
            return True
    return False


def shortlist_stream_candidates(
    stream_map: dict[str, dict[str, Any]],
    match: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Loại danh sách stream toàn cục và chỉ giữ nguồn có liên hệ với player trận hiện tại."""
    active_families: set[str] = set()
    blv_slug = (parse_qs(urlparse(match.get("url", "")).query).get("blv") or [""])[0]
    blv_family = re.sub(r"[^a-z0-9_-]+", "", blv_slug.lower())
    if blv_family:
        active_families.add(blv_family)

    for entry in stream_map.values():
        if _entry_is_browser_observed(entry):
            family = stream_family_key(entry.get("url", ""))
            if family:
                active_families.add(family)

    ranked: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    source_weights = {
        "http/iframe": 132,
        "http/stream": 130,
        "http/reference": 118,
        "response": 120,
        "iframe/src": 115,
        "hls/variant": 110,
        "home-card/stream-hint": 108,
        "previous-playlist": 82,
        "metadata/quality-source": 72,
        "response/body": 5,
    }

    for original in stream_map.values():
        entry = dict(original)
        entry["sources"] = list(original.get("sources") or [])
        entry["url"] = canonicalize_stream_url(entry.get("url", ""))
        if not is_direct_stream_url(entry["url"], entry.get("content_type", "")):
            entry["reject_reason"] = "URL media không hợp lệ"
            rejected.append(entry)
            continue

        family = stream_family_key(entry["url"])
        sources = entry.get("sources") or []
        only_body = bool(sources) and all(source == "response/body" for source in sources)
        is_previous = "previous-playlist" in sources
        is_observed = _entry_is_browser_observed(entry)
        if only_body and family not in active_families:
            entry["reject_reason"] = "chỉ xuất hiện trong response body toàn cục, không thuộc player hiện tại"
            rejected.append(entry)
            continue
        if active_families and family and family not in active_families and not is_previous and not is_observed:
            entry["reject_reason"] = "khác family stream đang được player trận hiện tại sử dụng"
            rejected.append(entry)
            continue

        score = 0
        for source in sources:
            if source.startswith("http/"):
                score = max(score, 130)
            elif source.startswith("request/"):
                score = max(score, 125)
            elif source.startswith("dom/"):
                score = max(score, 100)
            elif source.startswith("quality/"):
                score = max(score, 105)
            else:
                score = max(score, source_weights.get(source, 20))
        statuses = [int(value) for value in (entry.get("statuses") or [])]
        if any(value in {200, 206} for value in statuses):
            score += 35
        if any(value == 204 for value in statuses):
            score -= 45
        if any(value in {404, 410} for value in statuses):
            score -= 90
        if family and family in active_families:
            score += 55
        if blv_family and (family == blv_family or stream_channel_key(entry["url"]).startswith(blv_family)):
            score += 70
        if entry.get("quality"):
            score += 8
        if normalize_playback_referer(entry.get("referer", "")).startswith(PLAYER_ORIGIN_FALLBACK):
            score += 8

        entry["candidate_score"] = score
        entry["observed_active"] = _entry_is_browser_observed(entry)
        entry["high_confidence_observed"] = _entry_is_high_confidence_observed(entry)
        entry["channel_key"] = stream_channel_key(entry["url"])
        entry["family_key"] = family
        ranked.append(entry)

    # Khi URL trận chỉ rõ ?blv=..., chỉ dùng đúng family của BLV đó.
    # Không để link lịch sử hoặc request phụ của BLV khác lọt vào cùng trận.
    if blv_family:
        matching_family = [
            entry for entry in ranked
            if entry.get("family_key") == blv_family
            or str(entry.get("channel_key") or "").startswith(blv_family)
        ]
        if matching_family:
            for entry in ranked:
                if entry not in matching_family:
                    entry["reject_reason"] = f"khác BLV/family được chỉ định: {blv_family}"
                    rejected.append(entry)
            ranked = matching_family

    apply_paired_quality_hints(ranked)
    ranked.sort(
        key=lambda item: (
            int(item.get("candidate_score") or 0),
            bool(item.get("observed_active")),
            int(item.get("quality_rank") or quality_rank(item.get("quality", ""))),
        ),
        reverse=True,
    )

    # Giữ số lượng nhỏ để tránh tự tạo 429 trong bước xác minh.
    shortlisted: list[dict[str, Any]] = []
    per_channel: Counter[str] = Counter()
    for entry in ranked:
        channel = entry.get("channel_key") or entry["url"]
        if per_channel[channel] >= 2:
            entry["reject_reason"] = "trùng quá nhiều biến thể cùng channel"
            rejected.append(entry)
            continue
        shortlisted.append(entry)
        per_channel[channel] += 1
        if len(shortlisted) >= MAX_VERIFY_CANDIDATES:
            break

    for entry in ranked[len(shortlisted):]:
        if entry not in shortlisted and "reject_reason" not in entry:
            entry["reject_reason"] = "vượt giới hạn ứng viên xác minh"
            rejected.append(entry)
    return shortlisted, rejected


def _http_read_sample(
    url: str,
    headers: dict[str, str],
    timeout: int,
    max_bytes: int,
    range_header: str = "",
) -> dict[str, Any]:
    request_headers = dict(headers)
    request_headers.setdefault("Connection", "close")
    if range_header:
        request_headers["Range"] = range_header
    request = urllib.request.Request(url, headers=request_headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()) or 0)
            content_type = response.headers.get("Content-Type", "")
            response_headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            data = b""
            read_error = ""
            try:
                # HTTPResponse.read(n) có thể chờ đủ n byte trên live chunked và ném
                # timeout dù server đã trả HTTP 200 + video/x-flv. read1() trả chunk
                # hiện có sớm hơn; nếu body vẫn timeout thì vẫn giữ status/header.
                reader = getattr(response, "read1", None)
                data = reader(max_bytes) if callable(reader) else response.read(max_bytes)
            except Exception as exc:
                read_error = f"{type(exc).__name__}: {exc}"
            return {
                "status": status,
                "data": data,
                "content_type": content_type,
                "response_headers": response_headers,
                "final_url": response.geturl(),
                "error": read_error,
            }
    except urllib.error.HTTPError as exc:
        sample = b""
        try:
            sample = exc.read(min(max_bytes, 4096))
        except Exception:
            pass
        return {
            "status": int(exc.code or 0),
            "data": sample,
            "content_type": exc.headers.get("Content-Type", "") if exc.headers else "",
            "response_headers": {str(k).lower(): str(v) for k, v in exc.headers.items()} if exc.headers else {},
            "final_url": exc.geturl() or url,
            "error": f"HTTP {exc.code}",
        }
    except Exception as exc:
        return {
            "status": 0,
            "data": b"",
            "content_type": "",
            "response_headers": {},
            "final_url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _first_hls_uri(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    for line in lines:
        if not line.startswith("#"):
            return line
    # Hỗ trợ LL-HLS/fMP4 khi segment chỉ xuất hiện trong thuộc tính URI.
    for line in lines:
        if line.startswith(("#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:", "#EXT-X-MAP:")):
            match = re.search(r'URI="([^"]+)"', line, re.I)
            if match:
                return match.group(1)
    return ""


def _looks_like_error_page(data: bytes) -> bool:
    sample = data.lstrip()[:200].lower()
    return sample.startswith((b"<html", b"<!doctype", b"{\"error", b"access denied"))


def probe_stream_sync(
    url: str,
    user_agent: str,
    referer: str,
    origin: str = "",
    cookie_header: str = "",
    timeout: int = 8,
) -> dict[str, Any]:
    """Xác minh manifest/segment HLS hoặc chữ ký FLV bằng request có đúng header."""
    canonical = canonicalize_stream_url(url)
    kind = stream_kind(canonical)
    headers = {
        "User-Agent": user_agent or UA,
        "Referer": referer or PLAYER_ORIGIN_FALLBACK + "/",
        "Accept": "*/*",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    if origin:
        headers["Origin"] = origin
    if cookie_header:
        headers["Cookie"] = cookie_header

    if kind == "flv":
        result = _http_read_sample(
            canonical, headers, timeout, 64, range_header="bytes=0-63"
        )
        # Chỉ retry GET thường khi server phản hồi rõ 204/416. Không retry một
        # TimeoutError status=0 vì sẽ nhân đôi thời gian quét mọi FLV chưa phát.
        # Nếu HTTP 200 + video/x-flv nhưng body live tiếp tục stream, header đã đủ
        # xác nhận và cũng không cần mở thêm kết nối thứ hai.
        first_status = int(result.get("status") or 0)
        if first_status in {204, 416}:
            retry = _http_read_sample(canonical, headers, timeout, 64)
            if int(retry.get("status") or 0) or retry.get("data"):
                result = retry
        status = int(result.get("status") or 0)
        data = result.get("data") or b""
        ctype = str(result.get("content_type") or "").lower()
        header_confirms_flv = "flv" in ctype
        playable = status in {200, 206} and (
            data.startswith(b"FLV") or header_confirms_flv
        )
        state = "verified" if playable else (
            "blocked" if status in {401, 403, 429} else
            "dead" if status in {404, 410} else
            "empty" if status == 204 else
            "invalid"
        )
        return {
            **result,
            "playable": playable,
            "state": state,
            "kind": kind,
            "detail": (
                "FLV signature OK" if data.startswith(b"FLV") else
                "HTTP 200/206 + Content-Type FLV OK (live chunked)" if playable else
                result.get("error") or "không có chữ ký/content-type FLV"
            ),
        }

    if kind == "m3u8":
        manifest = _http_read_sample(canonical, headers, timeout, 768_000)
        status = int(manifest.get("status") or 0)
        data = manifest.get("data") or b""
        text = data.decode("utf-8", errors="ignore").lstrip("\ufeff\r\n \t")
        if status not in {200, 206}:
            state = "blocked" if status in {401, 403, 429} else "dead" if status in {404, 410} else "invalid"
            return {
                **manifest,
                "playable": False,
                "state": state,
                "kind": kind,
                "detail": manifest.get("error") or f"manifest HTTP {status}",
            }
        if not text.startswith("#EXTM3U"):
            return {
                **manifest,
                "playable": False,
                "state": "invalid",
                "kind": kind,
                "detail": "nội dung không bắt đầu bằng #EXTM3U",
            }

        first_uri = _first_hls_uri(text)
        if not first_uri:
            return {
                **manifest,
                "playable": False,
                "state": "empty",
                "kind": kind,
                "detail": "manifest chưa có variant/segment",
            }

        child_url = urljoin(str(manifest.get("final_url") or canonical), first_uri)
        if "#EXT-X-STREAM-INF" in text:
            child = _http_read_sample(child_url, headers, timeout, 768_000)
            child_status = int(child.get("status") or 0)
            child_text = (child.get("data") or b"").decode("utf-8", errors="ignore").lstrip("\ufeff\r\n \t")
            if child_status not in {200, 206} or not child_text.startswith("#EXTM3U"):
                return {
                    **manifest,
                    "playable": False,
                    "state": "blocked" if child_status in {401, 403, 429} else "invalid",
                    "kind": kind,
                    "detail": f"variant không tải được: HTTP {child_status}",
                    "child_url": child_url,
                }
            first_uri = _first_hls_uri(child_text)
            if not first_uri:
                return {
                    **manifest,
                    "playable": False,
                    "state": "empty",
                    "kind": kind,
                    "detail": "variant chưa có segment",
                    "child_url": child_url,
                }
            child_url = urljoin(str(child.get("final_url") or child_url), first_uri)

        segment = _http_read_sample(
            child_url, headers, timeout, 4096, range_header="bytes=0-4095"
        )
        if int(segment.get("status") or 0) in {204, 416} or not (segment.get("data") or b""):
            retry_segment = _http_read_sample(child_url, headers, timeout, 4096)
            if int(retry_segment.get("status") or 0) or retry_segment.get("data"):
                segment = retry_segment
        segment_status = int(segment.get("status") or 0)
        segment_data = segment.get("data") or b""
        playable = (
            segment_status in {200, 206}
            and len(segment_data) >= 64
            and not _looks_like_error_page(segment_data)
        )
        return {
            **manifest,
            "playable": playable,
            "state": "verified" if playable else (
                "blocked" if segment_status in {401, 403, 429} else
                "dead" if segment_status in {404, 410} else
                "invalid"
            ),
            "kind": kind,
            "detail": "manifest + segment OK" if playable else f"segment HTTP {segment_status}",
            "segment_url": child_url,
            "segment_status": segment_status,
            "segment_bytes": len(segment_data),
        }

    return {
        "playable": False,
        "state": "invalid",
        "kind": "",
        "status": 0,
        "detail": "không nhận diện được loại stream",
    }


async def validate_stream_candidates(
    context: BrowserContext,
    candidates: list[dict[str, Any]],
    match: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not candidates:
        return [], []
    if not VERIFY_STREAMS:
        for entry in candidates:
            entry["playability"] = "not-checked"
        return candidates[:MAX_OUTPUT_STREAMS_PER_MATCH], []

    semaphore = asyncio.Semaphore(2)

    async def validate_one(entry: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            referer = normalize_playback_referer(
                entry.get("referer") or PLAYER_ORIGIN_FALLBACK + "/"
            )
            user_agent = clean_text(entry.get("user_agent") or UA)
            origin = clean_text(entry.get("origin") or origin_from_url(match.get("url", "")))
            cookie_header = ""
            try:
                cookies = await context.cookies([entry["url"]])
                cookie_header = "; ".join(
                    f"{cookie.get('name')}={cookie.get('value')}" for cookie in cookies
                    if cookie.get("name")
                )
            except Exception:
                pass
            cache_key = (entry["url"], referer, user_agent, origin, cookie_header)
            cached = PROBE_CACHE.get(cache_key)
            if cached is None:
                probe = await asyncio.to_thread(
                    probe_stream_sync,
                    entry["url"],
                    user_agent,
                    referer,
                    origin,
                    cookie_header,
                    VERIFY_TIMEOUT_SECONDS,
                )
                sample_data = probe.pop("data", b"")
                probe["sample_bytes"] = len(sample_data) if isinstance(sample_data, (bytes, bytearray)) else 0
                if len(PROBE_CACHE) >= 500:
                    PROBE_CACHE.clear()
                PROBE_CACHE[cache_key] = dict(probe)
            else:
                probe = dict(cached)
                sample_data = b""
            probe.setdefault("sample_bytes", len(sample_data) if isinstance(sample_data, (bytes, bytearray)) else 0)
            entry["probe"] = probe
            entry["referer"] = referer
            entry["origin"] = origin
            entry["user_agent"] = user_agent
            return entry

    checked = await asyncio.gather(*(validate_one(dict(entry)) for entry in candidates))
    verified: list[dict[str, Any]] = []
    observed_fallback: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for entry in checked:
        probe = entry.get("probe") or {}
        state = probe.get("state", "invalid")
        status = int(probe.get("status") or 0)
        blocking_status = int(probe.get("segment_status") or probe.get("child_status") or status or 0)
        if probe.get("playable"):
            entry["playability"] = "verified"
            verified.append(entry)
            print(
                f"   ✅ ĐÃ XÁC MINH {stream_kind(entry['url']).upper()} | "
                f"HTTP {status} | {probe.get('detail')} | {entry['url']}",
                flush=True,
            )
            continue

        browser_statuses = {int(value) for value in (entry.get("statuses") or []) if str(value).isdigit()}
        sources = set(entry.get("sources") or [])
        current_observed_sources = {source for source in sources if source != "previous-playlist"}
        if (
            is_upcoming_within_window(match)
            and entry.get("high_confidence_observed")
            and current_observed_sources
            and state not in {"dead"}
            and blocking_status not in {404, 410}
            and int(entry.get("candidate_score") or 0) >= UPCOMING_MIN_CANDIDATE_SCORE
        ):
            entry["playability"] = "upcoming-pending"
            observed_fallback.append(entry)
            delta = int(match.get("minutes_to_kickoff") or 0)
            print(
                f"   🕒 Giữ link pending: trận bắt đầu sau {delta} phút | "
                f"{probe.get('detail') or state} | {entry['url']}",
                flush=True,
            )
            continue

        if (
            ALLOW_UNVERIFIED_BROWSER_FALLBACK
            and state == "blocked"
            and entry.get("high_confidence_observed")
            and blocking_status in {401, 403, 429}
            and any(value in {200, 206} for value in browser_statuses)
        ):
            entry["playability"] = "browser-observed"
            observed_fallback.append(entry)
            print(
                f"   🟡 Giữ URL vì browser đã nhận 200/206 nhưng probe riêng bị chặn: {entry['url']}",
                flush=True,
            )
            continue

        if (
            KEEP_PREVIOUS_UNVERIFIED
            and state == "blocked"
            and "previous-playlist" in (entry.get("sources") or [])
            and blocking_status in {401, 403, 429}
        ):
            entry["playability"] = "previous-fallback"
            observed_fallback.append(entry)
            print(
                f"   🟠 Giữ link lịch sử theo tùy chọn KEEP_PREVIOUS_UNVERIFIED: {entry['url']}",
                flush=True,
            )
            continue

        entry["playability"] = "rejected"
        entry["reject_reason"] = probe.get("detail") or state
        if "previous-playlist" in (entry.get("sources") or []) and not probe.get("playable"):
            entry["reject_reason"] = f"playlist cũ không còn xác minh được: {entry['reject_reason']}"
        rejected.append(entry)
        print(
            f"   ❌ Loại link không phát được | {entry.get('reject_reason')} | {entry['url']}",
            flush=True,
        )

    # Có link xác minh thật thì không trộn link mơ hồ vào playlist chính.
    if verified:
        for entry in observed_fallback:
            entry["reject_reason"] = "đã có stream xác minh thật nên không dùng fallback"
        rejected.extend(observed_fallback)
        selected = verified
    else:
        selected = observed_fallback
    selected.sort(
        key=lambda item: (
            PLAYABILITY_PRIORITY.get(str(item.get("playability", "")), 0),
            int(item.get("candidate_score") or 0),
            int(item.get("quality_rank") or quality_rank(item.get("quality", ""))),
        ),
        reverse=True,
    )
    return selected, rejected


async def finalize_stream_map(
    context: BrowserContext,
    stream_map: dict[str, dict[str, Any]],
    match: dict[str, Any],
    *,
    log_prefix: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates, pre_rejected = shortlist_stream_candidates(stream_map, match)
    print(
        f"   🔎 {log_prefix}Ứng viên sau lọc quan hệ player: {len(candidates)}/"
        f"{len(stream_map)}; loại sớm={len(pre_rejected)}",
        flush=True,
    )
    streams, validation_rejected = await validate_stream_candidates(context, candidates, match)
    rejected = pre_rejected + validation_rejected
    variant_parents = {
        entry.get("parent_url") for entry in streams
        if entry.get("parent_url") and entry.get("quality")
    }
    if variant_parents:
        streams = [
            entry for entry in streams
            if entry.get("url") not in variant_parents or entry.get("quality")
        ]
    streams, quality_rejected = select_best_quality_streams(streams, MAX_OUTPUT_STREAMS_PER_MATCH)
    rejected.extend(quality_rejected)
    streams = sorted(
        streams,
        key=lambda item: (
            PLAYABILITY_PRIORITY.get(str(item.get("playability", "")), 0),
            int(item.get("quality_rank") or quality_rank(item.get("quality", ""))),
            1 if stream_kind(item.get("url", "")) == "m3u8" else 0,
        ),
        reverse=True,
    )
    return streams, rejected


def match_id_from_url(value: str) -> str:
    parsed = urlparse(value or "")
    match = re.search(r"/s8-live/(\d+)(?:/|$)", parsed.path, re.I)
    if match:
        return match.group(1)
    query = parse_qs(parsed.query)
    values = query.get("s8_live_fixture_id") or query.get("fixture_id") or []
    return clean_text(values[0]) if values else ""


def extract_gavang_stream_key(value: str) -> str:
    """Lấy stream key công khai từ query hoặc slug /s8-live/<id>/<key>/.

    Chỉ chấp nhận ký tự an toàn để không biến dữ liệu trang thành đường dẫn tùy ý.
    """
    parsed = urlparse(decode_url_repeatedly(value or ""))
    query = parse_qs(parsed.query)
    candidates = list(query.get("s8_live_stream_key") or [])
    path_match = re.search(r"/s8-live/\d+/([^/?#]+)", parsed.path, re.I)
    if path_match:
        candidates.append(unquote(path_match.group(1)))
    for candidate in candidates:
        key = clean_text(candidate).strip("/ ")
        if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,180}", key):
            return key
    return ""


def gavang_match_identity(value: str) -> str:
    """Khóa ổn định để gộp các URL cùng fixture nhưng khác query/tham số."""
    fixture_id = match_id_from_url(value)
    stream_key = extract_gavang_stream_key(value)
    if fixture_id:
        return f"fixture:{fixture_id}"
    if stream_key:
        return f"stream:{stream_key.lower()}"
    parsed = urlparse(decode_url_repeatedly(value or ""))
    return f"url:{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"



GAVANG_STREAM_KEY_NOISE = {
    "ausffa", "auscup", "kork1", "kork2", "chnfa", "chnfacup", "finveik",
    "argcopa", "argcup", "c1qual", "uclqual", "uefaqual", "lbnprem",
    "uzbsuper", "ligaprosa", "jpnj1", "jpnj2", "thaprem", "viecup",
    "c3qual", "ueclqual", "intcf", "mexliga", "brasa", "affw", "mls",
    "kazdiv1", "uzbpro", "norelite", "braa", "brasera", "fraw",
}

# Chỉ mở rộng các token viết tắt đã quan sát rõ trong log Gà Vàng. Đây là
# fallback hiển thị; metadata exact-fixture/script và đối chiếu liên nguồn vẫn
# luôn được ưu tiên trước.
GAVANG_TEAM_TOKEN_ALIASES = {
    "camw": "Cambodia Women",
    "sinw": "Singapore Women",
    "sydnet58": "Sydney United 58 FC",
    "mariners": "Central Coast Mariners",
    "buncheon": "Bucheon FC 1995",
    "anyang": "FC Anyang",
    "cincinati": "FC Cincinnati",
    "vancouver": "Vancouver Whitecaps",
    "lagalaxy": "LA Galaxy",
    "stlouis": "St. Louis City SC",
    "tot": "Tottenham Hotspur",
    "mkdons": "MK Dons",
    "bodo": "Bodø/Glimt",
    "hamkam": "HamKam",
    "lillestrom": "Lillestrøm SK",
    "neftci": "Neftçi PFK",
}

# Token dùng riêng cho đối chiếu mềm. Các mã gộp như ``lagalaxy`` phải
# quy về token có thể gặp trong tên đầy đủ, nếu không metadata đúng lại bị
# đánh dấu trái stream key.
GAVANG_TOKEN_MATCH_ALIASES = {
    "camw": ["cambodia"],
    "sinw": ["singapore"],
    "sydnet58": ["sydney"],
    "buncheon": ["bucheon"],
    "cincinati": ["cincinnati"],
    "lagalaxy": ["galaxy"],
    "stlouis": ["louis"],
    "tot": ["tottenham"],
    "mkdons": ["dons"],
    "bodo": ["bodo"],
    "hamkam": ["hamkam"],
}


def _stream_key_from_media_url(value: str) -> str:
    key = extract_gavang_stream_key(value)
    if key:
        return key
    parsed = urlparse(decode_url_repeatedly(value or ""))
    stem = Path(parsed.path).stem
    if stem.lower() == "index":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2:
            stem = parts[-2]
    return clean_text(stem)


def gavang_stream_key_tokens(value: str) -> list[str]:
    """Tách token nhận diện trận từ stream key, chỉ dùng để chấm metadata.

    Đây là kiểm tra mềm: token không khớp không bao giờ làm mất stream đã verified.
    """
    key = _stream_key_from_media_url(value).lower()
    raw = [part for part in re.split(r"[^a-z0-9]+", key) if part]
    while raw and raw[-1] in GAVANG_STREAM_KEY_NOISE:
        raw = raw[:-1]
    expanded: list[str] = []
    for part in raw:
        if part in {"vs", "live", "stream"}:
            continue
        expanded.extend(GAVANG_TOKEN_MATCH_ALIASES.get(part, [part]))
    return [part for part in expanded if len(part) >= 3]


def title_stream_key_confidence(title: str, value: str) -> dict[str, Any]:
    key_tokens = gavang_stream_key_tokens(value)
    title_tokens = set(re.findall(r"[a-z0-9]+", normalize_search_text(title)))
    matched = [token for token in key_tokens if token in title_tokens]
    coverage = len(matched) / len(key_tokens) if key_tokens else 0.0
    contradictory = bool(re.search(r"\bvs\b", clean_text(title), re.I) and len(key_tokens) >= 2 and not matched)
    return {
        "key_tokens": key_tokens,
        "matched_tokens": matched,
        "match_count": len(matched),
        "coverage": coverage,
        "contradictory": contradictory,
    }


def _pretty_gavang_team_token(value: str) -> str:
    token = clean_text(value).lower()
    if token in GAVANG_TEAM_TOKEN_ALIASES:
        return GAVANG_TEAM_TOKEN_ALIASES[token]
    # Tách chữ-số để ``sydnet58`` không biến thành một chuỗi khó đọc nếu chưa
    # có alias; giữ các acronym phổ biến ở dạng in hoa.
    token = re.sub(r"(?<=[a-z])(?=\d)|(?<=\d)(?=[a-z])", " ", token)
    words = [part for part in re.split(r"[_\s]+", token) if part]
    rendered = []
    for word in words:
        if word in {"fc", "sc", "cf", "afc", "fk", "sk", "la", "mk"}:
            rendered.append(word.upper())
        else:
            rendered.append(word.title())
    return " ".join(rendered)


def gavang_display_key_tokens(value: str) -> list[str]:
    """Token nguyên bản cho tên fallback; không thay alias thành token đối chiếu."""
    key = _stream_key_from_media_url(value).lower()
    raw = [part for part in re.split(r"[^a-z0-9]+", key) if part]
    while raw and raw[-1] in GAVANG_STREAM_KEY_NOISE:
        raw = raw[:-1]
    return [part for part in raw if part not in {"vs", "live", "stream"} and len(part) >= 2]


def fallback_match_name_from_stream_key(value: str) -> str:
    tokens = gavang_display_key_tokens(value)
    if not tokens:
        return clean_match_name("", value)
    if len(tokens) == 1:
        return _pretty_gavang_team_token(tokens[0])
    # Stream key Gà Vàng hiện thường dùng một token rút gọn cho mỗi đội, sau đó là mã giải.
    # Fallback này chỉ dùng khi metadata exact-fixture không có; không bao giờ dùng để loại stream.
    return f"{_pretty_gavang_team_token(tokens[0])} VS {_pretty_gavang_team_token(tokens[1])}"


def sanitize_gavang_match_metadata(match: dict[str, Any], *, stage: str) -> dict[str, Any]:
    """Ngăn metadata của fixture khác ghi đè, nhưng tuyệt đối không loại URL stream."""
    title = clean_text(str(match.get("match_name") or match.get("raw_title") or ""))
    confidence = title_stream_key_confidence(title, str(match.get("url", "")))
    match["metadata_key_confidence"] = confidence
    if confidence["contradictory"]:
        warning = (
            f"{stage}: tên metadata không liên quan stream_key "
            f"({title!r} vs {confidence['key_tokens']!r}); giữ link, bỏ metadata nghi ghép nhầm"
        )
        match.setdefault("metadata_warnings", []).append(warning)
        fallback = fallback_match_name_from_stream_key(str(match.get("url", "")))
        if fallback:
            match["match_name"] = fallback
            match["raw_title"] = fallback
        # BLV/logo từ cùng khối sai fixture cũng không đáng tin. Giờ chỉ giữ nếu đã có nguồn thuộc tính rõ.
        match["blv"] = ""
        match["raw_blv"] = ""
        match["raw_time"] = ""
        match["time"] = ""
        match["date"] = ""
        match["logo"] = ""
    return confidence


def _match_title_score(value: str) -> tuple[int, int]:
    text = clean_text(value)
    if not text:
        return (0, 0)
    explicit_vs = 1 if re.search(r"\bvs\b", text, re.I) else 0
    slug_like = 1 if re.fullmatch(r"[a-z0-9 ._-]+", text) and not explicit_vs else 0
    return (explicit_vs * 100 - slug_like * 25, min(len(text), 300))


def dedupe_home_links(links: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Gộp card/anchor trùng fixture và giữ metadata giàu nhất.

    smorf.io có thể lặp cùng một fixture ở nhiều tab/khối và khác query như
    ``s8_auto_sound``. Dedupe theo fixture/stream key giúp không probe cùng FLV
    nhiều lần và không ghi metadata nghèo đè lên metadata đúng.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    duplicates = 0

    def choose_text(current: str, candidate: str, *, title: bool = False) -> str:
        current = clean_text(current)
        candidate = clean_text(candidate)
        if not candidate:
            return current
        if not current:
            return candidate
        if title:
            return candidate if _match_title_score(candidate) > _match_title_score(current) else current
        if extract_time(candidate) and not extract_time(current):
            return candidate
        return candidate if len(candidate) > len(current) else current

    for raw in links:
        item = dict(raw)
        identity = gavang_match_identity(str(item.get("url", "")))
        if identity not in merged:
            merged[identity] = item
            order.append(identity)
            continue

        duplicates += 1
        target = merged[identity]
        target["raw_title"] = choose_text(target.get("raw_title", ""), item.get("raw_title", ""), title=True)
        target["card_text"] = choose_text(target.get("card_text", ""), item.get("card_text", ""))
        target["raw_time"] = choose_text(target.get("raw_time", ""), item.get("raw_time", ""))
        target["raw_blv"] = choose_text(target.get("raw_blv", ""), item.get("raw_blv", ""))
        target["sport_hint"] = choose_text(target.get("sport_hint", ""), item.get("sport_hint", ""))

        # Ưu tiên URL có đủ query công khai để Referer giống trình duyệt thật.
        if len(str(item.get("url", ""))) > len(str(target.get("url", ""))):
            target["url"] = item.get("url", target.get("url", ""))

        for key in ("team_logos", "logo_candidates", "stream_hints"):
            combined: list[Any] = []
            seen: set[str] = set()
            for value in list(target.get(key) or []) + list(item.get(key) or []):
                marker = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, dict) else str(value)
                if marker in seen:
                    continue
                seen.add(marker)
                combined.append(value)
            target[key] = combined

        if not target.get("logo") and item.get("logo"):
            target["logo"] = item.get("logo")

    output = [merged[key] for key in order]
    for item in output:
        item.setdefault("match_name", clean_match_name(str(item.get("raw_title", "")), str(item.get("url", ""))))
        sanitize_gavang_match_metadata(item, stage="home-card")
        # fetch_stream sẽ chuẩn hóa lại match_name; giữ raw_title đã được bảo vệ ngay từ đây.
        item["raw_title"] = clean_text(str(item.get("match_name") or item.get("raw_title") or ""))
    return output, duplicates


def derived_gavang_stream_candidates(match_url: str) -> list[dict[str, str]]:
    """Dựng ứng viên FLV theo mẫu client-side quan sát được của Gà Vàng.

    URL dựng ra vẫn phải qua probe chữ ký FLV trước khi được ghi playlist.
    """
    key = extract_gavang_stream_key(match_url)
    if not key:
        return []
    origin = origin_from_url(match_url) or PLAYER_ORIGIN_FALLBACK
    return [{
        "url": urljoin(GAVANG_STREAM_BASE, key + ".flv"),
        "referer": match_url,
        "origin": origin,
        "quality": "",
        "source": "derived/s8_live_stream_key",
    }]


def derived_pending_reason(match: dict[str, Any]) -> str:
    """Cho phép giữ FLV dựng chưa phát trong cửa sổ an toàn.

    Đây là chính sách riêng của Gà Vàng: URL FLV không có token và được tạo trực tiếp
    từ stream_key công khai. Link verified vẫn luôn được ưu tiên; pending chỉ là cầu nối
    để đến giờ CDN mở luồng thì người dùng tải lại/mở lại kênh có thể xem ngay.
    """
    if not KEEP_DERIVED_PENDING or not extract_gavang_stream_key(str(match.get("url", ""))):
        return ""

    delta = match.get("minutes_to_kickoff")
    if isinstance(delta, int):
        if -SCAN_PAST_MINUTES <= delta <= SCAN_FUTURE_MINUTES:
            return "started-window" if delta < 0 else "scheduled-window"
        return ""

    reason = clean_text(str(match.get("scan_window_reason", "")))
    if reason == "unknown-time-live":
        return "explicit-live-no-time"
    if KEEP_UNKNOWN_TIME_PENDING and reason == "unknown-time-derived-probe":
        # URL vẫn đang được trang chủ hiện tại quảng bá, nên giữ dạng pending.
        # Khi URL biến mất khỏi trang chủ ở lần chạy sau, playlist tạm cũng biến mất.
        return "current-home-stream-key-no-time"
    return ""


def build_derived_pending_streams(
    match: dict[str, Any],
    candidates: list[dict[str, str]],
    rejected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Chuyển FLV dựng chưa phát thành `upcoming-pending` mà không giả là verified."""
    reason = derived_pending_reason(match)
    if not reason:
        return []

    rejected_by_url = {
        canonicalize_stream_url(str(item.get("url", ""))): item
        for item in rejected
        if isinstance(item, dict) and item.get("url")
    }
    pending: list[dict[str, Any]] = []
    for candidate in candidates:
        url = canonicalize_stream_url(str(candidate.get("url", "")))
        if not url or stream_kind(url) != "flv":
            continue
        entry = dict(rejected_by_url.get(url) or {})
        entry.update({
            "url": url,
            "referer": normalize_playback_referer(candidate.get("referer") or match.get("url", "")),
            "origin": clean_text(candidate.get("origin") or origin_from_url(str(match.get("url", "")))),
            "user_agent": clean_text(entry.get("user_agent") or UA),
            "content_type": clean_text(entry.get("content_type") or "video/x-flv"),
            "quality": normalize_quality_hint(entry.get("quality") or candidate.get("quality", "")),
            "sources": list(dict.fromkeys(list(entry.get("sources") or []) + [candidate.get("source", "derived/s8_live_stream_key")])),
            "playability": "upcoming-pending",
            "derived_pending": True,
            "pending_reason": reason,
            "candidate_score": max(int(entry.get("candidate_score") or 0), 220),
            "high_confidence_observed": False,
            "observed_active": False,
        })
        probe = dict(entry.get("probe") or {})
        probe["pending_retained"] = True
        probe["pending_reason"] = reason
        entry["probe"] = probe
        pending.append(entry)
    return pending[:1]

def _parse_previous_playlist_text(text: str, source_label: str) -> dict[str, list[dict[str, str]]]:
    mapping: dict[str, list[dict[str, str]]] = {}
    current_match_id = ""
    referer = PLAYER_ORIGIN_FALLBACK + "/"
    user_agent = UA
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line.startswith("#EXTINF"):
            id_match = re.search(r'tvg-id="gavang-(\d+)-\d+"', line)
            current_match_id = id_match.group(1) if id_match else ""
            referer = PLAYER_ORIGIN_FALLBACK + "/"
            user_agent = UA
        elif line.startswith("#EXTVLCOPT:http-referrer="):
            referer = line.split("=", 1)[1].strip()
        elif line.startswith("#EXTVLCOPT:http-user-agent="):
            user_agent = line.split("=", 1)[1].strip()
        elif line.startswith(("http://", "https://")) and current_match_id:
            url = canonicalize_stream_url(line.split("|", 1)[0])
            if is_direct_stream_url(url):
                mapping.setdefault(current_match_id, []).append({
                    "url": url,
                    "referer": referer,
                    "user_agent": user_agent,
                    "history_source": source_label,
                })
            current_match_id = ""
    return mapping


def load_previous_playlist_streams(path: str = OUTPUT_M3U) -> dict[str, list[dict[str, str]]]:
    """Đọc playlist hiện tại và tối đa 2 commit trước để cứu link từng chạy tốt."""
    sources: list[tuple[str, str]] = []
    playlist = Path(path)
    try:
        git_playlist_path = playlist.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        git_playlist_path = playlist.as_posix()
    if playlist.exists():
        try:
            sources.append(("working-tree", playlist.read_text(encoding="utf-8", errors="ignore")))
        except Exception:
            pass

    # Đọc cả đường dẫn hiện tại và layout thư mục của v4.4.1 để lần nâng cấp
    # đầu tiên không mất ứng viên lịch sử. Mọi link vẫn phải qua probe lại.
    git_playlist_paths = [git_playlist_path]
    if LEGACY_GIT_PLAYLIST_PATH not in git_playlist_paths:
        git_playlist_paths.append(LEGACY_GIT_PLAYLIST_PATH)
    for revision in ("HEAD~1", "HEAD~2"):
        for history_path in git_playlist_paths:
            try:
                completed = subprocess.run(
                    ["git", "show", f"{revision}:{history_path}"],
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=5,
                )
                if completed.returncode == 0 and completed.stdout.strip():
                    sources.append((f"{revision}:{history_path}", completed.stdout))
            except Exception:
                continue

    merged: dict[str, list[dict[str, str]]] = {}
    seen: set[tuple[str, str]] = set()
    for source_label, text in sources:
        parsed = _parse_previous_playlist_text(text, source_label)
        for match_id, items in parsed.items():
            for item in items:
                key = (match_id, item["url"])
                if key in seen:
                    continue
                seen.add(key)
                merged.setdefault(match_id, []).append(item)
    return merged


def is_direct_stream_url(url: str, content_type: str = "") -> bool:
    if not url:
        return False
    clean = canonicalize_stream_url(url)
    parsed = urlparse(clean)
    lower_url = clean.lower()
    if parsed.scheme not in {"http", "https"}:
        return False
    if not stream_kind(clean, content_type):
        return False
    return not any(marker in lower_url for marker in AD_MARKERS)


def extract_stream_urls(raw_url: str, content_type: str = "") -> list[str]:
    """Tách luồng trực tiếp, kể cả streamUrl đã percent-encode trong iframe embed."""
    if not raw_url:
        return []

    pending = [raw_url]
    seen_values: set[str] = set()
    found: list[str] = []
    nested_param_names = {
        "streamurl", "stream_url", "stream", "url", "src", "file",
        "source", "video", "hls", "flv", "playurl", "play_url",
    }

    while pending and len(seen_values) < 60:
        value = decode_url_repeatedly(pending.pop(0))
        if not value or value in seen_values:
            continue
        seen_values.add(value)

        direct_type = content_type if value == decode_url_repeatedly(raw_url) else ""
        canonical = canonicalize_stream_url(value)
        if is_direct_stream_url(canonical, direct_type):
            if canonical not in found:
                found.append(canonical)
            continue

        try:
            query = parse_qs(urlparse(value).query, keep_blank_values=False)
        except Exception:
            query = {}

        for key, values in query.items():
            if key.lower() not in nested_param_names:
                continue
            for nested in values:
                decoded = decode_url_repeatedly(nested)
                if decoded.startswith(("http://", "https://")):
                    pending.append(decoded)

        for match in re.findall(
            r"https?://[^\s\"'<>]+?(?:\.m3u8|\.flv)(?:\?[^\s\"'<>]*)?",
            decode_url_repeatedly(value),
            flags=re.IGNORECASE,
        ):
            pending.append(match.rstrip("),];"))

    return found


def stream_referer_hint(raw_candidate: str, frame_url: str = "") -> str:
    """Ưu tiên origin của iframe embed chứa streamUrl, không dùng nhầm trang trận."""
    decoded = decode_url_repeatedly(raw_candidate)
    if extract_stream_urls(decoded) and not is_direct_stream_url(decoded):
        embedded_origin = origin_from_url(decoded)
        if embedded_origin:
            return embedded_origin + "/"
    if frame_url:
        frame_origin = origin_from_url(frame_url)
        if frame_origin:
            return frame_origin + "/"
    return ""


def normalize_playback_referer(value: str) -> str:
    """Gà Vàng gửi full URL phòng làm Referer; không rút về trang chủ."""
    candidate = decode_url_repeatedly(value)
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    return PLAYER_ORIGIN_FALLBACK + "/"

def clean_match_name(value: str, fallback_url: str) -> str:
    text = clean_text(value)
    # Ưu tiên dòng/đoạn chứa "vs" nếu card còn kèm giải, thời gian, trạng thái.
    pieces = [clean_text(p) for p in re.split(r"[\n|]", value or "") if clean_text(p)]
    vs_piece = next((p for p in pieces if re.search(r"\bvs\b", p, re.I)), "")
    if vs_piece:
        text = vs_piece

    text = TIME_RE.sub(" ", text)
    text = re.sub(
        r"(?i)\b(xem ngay|trực tiếp|hot|live|bóng đá|sắp diễn ra|đang diễn ra|gavang|gà vàng)\b",
        " ",
        text,
    )
    text = clean_text(text).strip(" -|•")

    if not re.search(r"\bvs\b", text, re.I):
        # Không xuất nguyên slug kỹ thuật kiểu ``buncheon anyang kork1``.
        # Dùng stream_key để bỏ mã giải cuối và dựng tên hai đội dễ đọc.
        safe_fallback = fallback_match_name_from_stream_key(fallback_url)
        if safe_fallback:
            text = safe_fallback
        else:
            slug = unquote(urlparse(fallback_url).path.rstrip("/").split("/")[-1])
            slug = re.sub(r"-\d{2}-\d{2}-\d{4}-\d{4}$", "", slug)
            slug = re.sub(r"-vs-", " vs ", slug, flags=re.I)
            slug = re.sub(r"-(?:finveik|wc|live)$", "", slug, flags=re.I)
            slug = slug.replace("-", " ")
            text = clean_text(slug).title()

    return text or fallback_url


def derive_match_info(
    url: str,
    raw_title: str = "",
    raw_time: str = "",
) -> tuple[str, str, str]:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    match_name = clean_match_name(raw_title, url)
    time_str = extract_time(raw_time) or extract_time(raw_title)

    if not time_str:
        suffix = re.search(r"-(\d{2})(\d{2})/?$", parsed.path)
        if suffix:
            time_str = f"{suffix.group(1)}:{suffix.group(2)}"

    blv_name = ""
    for key in ("blvName", "blv_name", "commentator", "commentatorName", "blv"):
        values = query.get(key) or query.get(key.lower())
        if values:
            blv_name = normalize_blv_name(values[0])
            if blv_name:
                break

    return match_name, time_str, blv_name


def is_good_logo_url(value: str) -> bool:
    normalized = normalize_logo_url(value)
    lower = normalized.lower()
    if not normalized:
        return False
    bad = (
        "avatar", "banner", "advert", "doubleclick", "googleads", "emoji",
        "flag", "favicon", "placeholder", "default-avatar", "no-image",
        "logo-white", "logo-dark", "site-logo", "loading.gif",
    )
    return not any(marker in lower for marker in bad)


def is_good_source_logo_url(value: str) -> bool:
    normalized = normalize_logo_url(value)
    if not normalized:
        return False
    lower = normalized.lower()
    return not any(marker in lower for marker in (
        "avatar", "advert", "doubleclick", "googleads", "emoji",
        "placeholder", "default-avatar", "no-image", "loading.gif",
    ))


def choose_source_logo(candidates: list[Any], base: str = TARGET_URL) -> str:
    ranked: list[tuple[float, str]] = []
    seen: set[str] = set()
    for value in candidates:
        item = value if isinstance(value, dict) else {"url": value}
        url = normalize_logo_url(item, base)
        if not url or url in seen or not is_good_source_logo_url(url):
            continue
        seen.add(url)
        context = normalize_search_text(
            f"{item.get('context', '')} {item.get('source', '')} {urlparse(url).path}"
        )
        try:
            score = float(item.get("score") or 0)
        except Exception:
            score = 0.0
        if any(token in context for token in (" gavang ", " ga vang ", " site logo ", " header ", " footer ")):
            score += 80
        if " logo " in context:
            score += 20
        if " favicon " in context or urlparse(url).path.lower().endswith("favicon.ico"):
            score += 5
        ranked.append((score, url))
    if ranked:
        ranked.sort(reverse=True)
        return ranked[0][1]
    return default_gavang_source_logo(base)


def _team_parts(match_name: str) -> tuple[str, str]:
    parts = re.split(r"(?i)\s+vs\s+", clean_text(match_name), maxsplit=1)
    home = parts[0] if parts else ""
    away = parts[1].split(" - ", 1)[0] if len(parts) > 1 else ""
    return home, away


def _candidate_dict(value: Any, base: str) -> dict[str, Any]:
    if isinstance(value, dict):
        context = clean_text(str(value.get("context") or ""))
        source = clean_text(str(value.get("source") or ""))
        try:
            score = float(value.get("score") or 0)
        except Exception:
            score = 0.0
    else:
        context = ""
        source = ""
        score = 0.0
    return {
        "url": normalize_logo_url(value, base),
        "context": context,
        "source": source,
        "score": score,
    }


def _logo_context_and_hits(
    candidate: dict[str, Any],
    match_name: str,
) -> tuple[str, int, int]:
    url = candidate.get("url", "")
    context = normalize_search_text(
        f"{candidate.get('context', '')} {urlparse(url).path}"
    )
    home, away = _team_parts(match_name)
    home_tokens = [
        token for token in normalize_search_text(home).split()
        if len(token) >= 4
    ]
    away_tokens = [
        token for token in normalize_search_text(away).split()
        if len(token) >= 4
    ]
    home_hits = sum(1 for token in home_tokens if f" {token} " in f" {context} ")
    away_hits = sum(1 for token in away_tokens if f" {token} " in f" {context} ")
    return context, home_hits, away_hits


def score_logo_candidate(candidate: dict[str, Any], match_name: str) -> float:
    url = candidate.get("url", "")
    if not is_good_logo_url(url):
        return -10000

    score = float(candidate.get("score") or 0)
    context, home_hits, away_hits = _logo_context_and_hits(candidate, match_name)
    source = clean_text(str(candidate.get("source") or "")).lower()

    if home_hits:
        score += 55 + min(home_hits, 3) * 8
    elif away_hits:
        score += 35 + min(away_hits, 3) * 6

    if any(marker in f" {context} " for marker in (
        " team ", " club ", " home ", " away ", " doi ", " đội "
    )):
        score += 10

    if any(marker in f" {context} " for marker in (
        " avatar ", " blv ", " commentator ", " banner ", " league ",
        " sponsor ", " advert ", " quảng cáo "
    )):
        score -= 45

    # Ảnh lấy từ card/trang trận nhưng không hề có dấu hiệu thuộc hai đội rất dễ là
    # logo của một trận liên quan nằm cùng section. Thà bỏ trống còn hơn gán sai.
    if not home_hits and not away_hits:
        if source in {"home-card", "detail-match", "detail-team"}:
            score -= 18
        else:
            score -= 40

    if source == "detail-team":
        score += 10
    elif source == "detail-match":
        score += 5
    elif source == "meta":
        score -= 25

    return score


def ranked_logo_candidates(
    candidates: list[Any],
    base: str,
    match_name: str = "",
) -> list[dict[str, Any]]:
    # Cùng một URL có thể xuất hiện từ card trang chủ, DOM trang trận và metadata.
    # Giữ bản có context/điểm tốt nhất thay vì giữ lần xuất hiện đầu tiên.
    best_by_url: dict[str, dict[str, Any]] = {}
    for value in candidates:
        item = _candidate_dict(value, base)
        if not item["url"]:
            continue
        _context, home_hits, away_hits = _logo_context_and_hits(item, match_name)
        item["home_hits"] = home_hits
        item["away_hits"] = away_hits
        item["final_score"] = score_logo_candidate(item, match_name)
        if item["final_score"] <= -1000:
            continue
        previous = best_by_url.get(item["url"])
        if previous is None or (
            item["final_score"], item.get("home_hits", 0), item.get("away_hits", 0)
        ) > (
            previous["final_score"], previous.get("home_hits", 0), previous.get("away_hits", 0)
        ):
            best_by_url[item["url"]] = item

    ranked = list(best_by_url.values())
    ranked.sort(
        key=lambda item: (
            item["final_score"],
            item.get("home_hits", 0),
            item.get("away_hits", 0),
        ),
        reverse=True,
    )
    return ranked


def choose_logo(candidates: list[Any], base: str, match_name: str = "") -> str:
    ranked = ranked_logo_candidates(candidates, base, match_name)
    if not ranked:
        return ""
    best = ranked[0]
    # Chỉ dùng khi có dấu hiệu rõ ràng ảnh thuộc đội/trận hiện tại.
    if best["final_score"] < 28:
        return ""
    if not best.get("home_hits") and not best.get("away_hits") and best["final_score"] < 45:
        return ""
    return best["url"]


def resolve_duplicate_logos(results: list[dict[str, Any]]) -> None:
    """Giữ logo lặp cho đúng trận nhất, loại khỏi các trận bị gán nhầm."""
    ranked_by_result: dict[int, list[dict[str, Any]]] = {}
    for result in results:
        candidates = list(result.get("logo_candidates") or [])
        candidates.extend(result.get("team_logos") or [])
        if result.get("logo"):
            candidates.append(result["logo"])
        ranked = ranked_logo_candidates(
            candidates,
            result.get("url") or TARGET_URL,
            result.get("match_name") or "",
        )
        ranked_by_result[id(result)] = ranked
        result["logo"] = choose_logo(
            candidates,
            result.get("url") or TARGET_URL,
            result.get("match_name") or "",
        )

    usage: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        if result.get("logo"):
            usage.setdefault(result["logo"], []).append(result)

    reserved: set[str] = set()
    for logo_url, owners in usage.items():
        home_teams = {
            normalize_search_text(_team_parts(owner.get("match_name", ""))[0]).strip()
            for owner in owners
        }
        if len(owners) < 2 or len(home_teams) <= 1:
            reserved.add(logo_url)
            continue

        scored_owners: list[tuple[float, int, int, dict[str, Any]]] = []
        for owner in owners:
            candidate = next(
                (item for item in ranked_by_result[id(owner)] if item["url"] == logo_url),
                None,
            )
            if candidate:
                scored_owners.append((
                    float(candidate.get("final_score") or -9999),
                    int(candidate.get("home_hits") or 0),
                    int(candidate.get("away_hits") or 0),
                    owner,
                ))
            else:
                scored_owners.append((-9999.0, 0, 0, owner))

        scored_owners.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        top = scored_owners[0]
        second_score = scored_owners[1][0] if len(scored_owners) > 1 else -9999
        winner: dict[str, Any] | None = None
        if (
            top[0] >= 45
            and (top[1] > 0 or top[2] > 0)
            and (top[0] - second_score >= 12 or second_score < 28)
        ):
            winner = top[3]
            reserved.add(logo_url)

        print(
            f"   ⚠️ Phát hiện một logo bị gán cho {len(owners)} trận khác nhau; "
            f"giữ cho trận khớp nhất và chọn lại các trận còn lại: {logo_url}",
            flush=True,
        )

        for owner in owners:
            if owner is winner:
                continue
            alternatives = [
                item for item in ranked_by_result[id(owner)]
                if item["url"] != logo_url
                and item["url"] not in reserved
                and item["final_score"] >= 28
                and (item.get("home_hits") or item.get("away_hits"))
            ]
            owner["logo"] = alternatives[0]["url"] if alternatives else ""
            if owner["logo"]:
                reserved.add(owner["logo"])



async def install_route_filter(page: Page, homepage: bool = False) -> None:
    """Cho ảnh tải để lazy-load logo hoạt động; chỉ chặn font và media ở trang chủ."""
    blocked_types = {"font"}
    if homepage:
        blocked_types.add("media")

    async def route_handler(route: Route) -> None:
        if route.request.resource_type in blocked_types:
            await route.abort()
        else:
            await route.continue_()

    await page.route("**/*", route_handler)


async def collect_dom_stream_candidates(page: Page) -> list[dict[str, str]]:
    """Chỉ lấy nguồn đang được player sử dụng; không quét regex toàn bộ HTML/script."""
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for frame in page.frames:
        try:
            frame_candidates = await frame.evaluate(
                r"""() => {
                    const out = [];
                    const seen = new Set();
                    const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                    const qualityOf = (v) => {
                        const text = clean(v);
                        const match = text.match(/\b(4K|UHD|2160p?|Full\s*HD|FHD|1080p?|HD|720p?|SD|480p?|Auto)\b/i);
                        return match ? match[1] : "";
                    };
                    const add = (value, source, quality = "", context = "") => {
                        if (!value) return;
                        const raw = String(value).trim();
                        if (!raw || raw.length > 12000) return;
                        const key = `${raw}\n${source}\n${quality}`;
                        if (seen.has(key)) return;
                        seen.add(key);
                        out.push({
                            url: raw,
                            source,
                            quality: qualityOf(quality || context),
                            context: clean(context),
                        });
                    };

                    // Resource Timing chỉ chứa request đã thực sự được trình duyệt phát ra.
                    try {
                        for (const entry of performance.getEntriesByType("resource")) {
                            if (entry && entry.name && /\.m3u8|\.flv/i.test(entry.name)) {
                                add(entry.name, "performance");
                            }
                        }
                    } catch (_) {}

                    // Nguồn đang gắn trực tiếp vào player.
                    document.querySelectorAll("video, source").forEach((el) => {
                        const context = clean([
                            el.getAttribute("data-quality"),
                            el.getAttribute("data-resolution"),
                            el.getAttribute("aria-label"),
                            el.title,
                            el.className,
                        ].filter(Boolean).join(" "));
                        const quality = qualityOf(context);
                        [
                            el.currentSrc,
                            el.src,
                            el.getAttribute("src"),
                            el.getAttribute("data-src"),
                            el.getAttribute("data-url"),
                            el.getAttribute("data-stream"),
                            el.getAttribute("data-stream-url"),
                            el.getAttribute("data-file"),
                        ].forEach((value) => add(value, "media-element", quality, context));
                    });

                    // Iframe active thường chứa streamUrl đã percent-encode.
                    document.querySelectorAll("iframe[src]").forEach((el) => {
                        const context = clean([
                            el.getAttribute("title"),
                            el.getAttribute("aria-label"),
                            el.className,
                        ].filter(Boolean).join(" "));
                        add(el.src || el.getAttribute("src"), "iframe", qualityOf(context), context);
                    });

                    // Chỉ đọc data-* của phần tử đang active/selected/visible trong player.
                    document.querySelectorAll(
                        "[data-stream], [data-stream-url], [data-hls], [data-flv], [data-file], [data-url]"
                    ).forEach((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        const active = el.matches(
                            ".active, .selected, [aria-selected='true'], [aria-current='true'], :checked"
                        );
                        const visible = rect.width > 0 && rect.height > 0 &&
                            style.display !== "none" && style.visibility !== "hidden";
                        if (!active && !visible) return;
                        const context = clean([
                            el.innerText, el.textContent, el.getAttribute("aria-label"),
                            el.getAttribute("title"), el.getAttribute("data-quality"),
                            el.getAttribute("data-resolution"), el.className,
                        ].filter(Boolean).join(" "));
                        const quality = qualityOf(context);
                        [
                            el.getAttribute("data-stream"),
                            el.getAttribute("data-stream-url"),
                            el.getAttribute("data-hls"),
                            el.getAttribute("data-flv"),
                            el.getAttribute("data-file"),
                            el.getAttribute("data-url"),
                        ].forEach((value) => add(value, "active-data", quality, context));
                    });
                    return out.slice(0, 120);
                }"""
            )
            for item in frame_candidates:
                raw_url = str(item.get("url", "")) if isinstance(item, dict) else str(item)
                quality = str(item.get("quality", "")) if isinstance(item, dict) else ""
                source = str(item.get("source", "dom")) if isinstance(item, dict) else "dom"
                key = (raw_url, frame.url or "", source)
                if raw_url and key not in seen:
                    seen.add(key)
                    candidates.append({
                        "url": raw_url,
                        "frame_url": frame.url or "",
                        "quality": normalize_quality_hint(quality),
                        "source": source,
                    })
        except Exception:
            continue

    return candidates


async def stimulate_player(page: Page) -> None:
    for selector in PLAY_SELECTORS:
        try:
            locator = page.locator(selector)
            if await locator.count():
                await locator.first.click(timeout=700, force=True)
        except Exception:
            pass

    for frame in page.frames:
        try:
            await frame.evaluate(
                """() => {
                    document.querySelectorAll("video").forEach((video) => {
                        try {
                            video.muted = true;
                            video.volume = 0;
                            const result = video.play();
                            if (result && typeof result.catch === "function") {
                                result.catch(() => {});
                            }
                        } catch (_) {}
                    });
                }"""
            )
        except Exception:
            pass


async def stimulate_quality_variants(page: Page) -> int:
    """Mở menu chất lượng và lần lượt kích hoạt HD/FHD/1080 để lộ mọi URL."""
    clicked = 0
    for frame in page.frames:
        try:
            count = await frame.evaluate(
                r"""async () => {
                    const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                    const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== "none" && style.visibility !== "hidden";
                    };
                    const nodes = Array.from(document.querySelectorAll(
                        "button, a, [role='button'], [role='option'], li, label, [data-quality], [data-resolution]"
                    )).filter((el) => {
                        if (el.tagName !== "A") return true;
                        const href = String(el.getAttribute("href") || "").trim();
                        return !href || href === "#" || href.startsWith("javascript:");
                    });
                    const textOf = (el) => clean([
                        el.innerText, el.textContent, el.getAttribute("aria-label"),
                        el.getAttribute("title"), el.getAttribute("data-quality"),
                        el.getAttribute("data-resolution"), el.className
                    ].filter(Boolean).join(" "));

                    let clicks = 0;
                    const menu = nodes.find((el) => visible(el) && /quality|chất lượng|độ phân giải/i.test(textOf(el)));
                    if (menu) {
                        try { menu.click(); clicks += 1; await delay(300); } catch (_) {}
                    }

                    const options = nodes.filter((el) =>
                        visible(el) && /\b(4K|UHD|2160p?|Full\s*HD|FHD|1080p?|HD|720p?|SD|480p?)\b/i.test(textOf(el))
                    ).slice(0, 10);
                    for (const option of options) {
                        try { option.click(); clicks += 1; await delay(450); } catch (_) {}
                    }
                    return clicks;
                }"""
            )
            clicked += int(count or 0)
        except Exception:
            continue
    return clicked


async def scan_quality_variants(page: Page, capture_callback: Any) -> list[str]:
    """Bấm từng chất lượng và chỉ ghi URL mới xuất hiện sau lần bấm đó."""
    discovered: list[str] = []

    for frame in list(page.frames):
        try:
            labels = await frame.evaluate(
                r"""() => {
                    const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                    const qualityOf = (value) => {
                        const text = clean(value);
                        if (/\b(4K|UHD|2160p?)\b/i.test(text)) return "4K";
                        if (/\b(Full\s*HD|FHD|1080p?)\b/i.test(text)) return "FHD";
                        if (/\b(HD|720p?)\b/i.test(text)) return "HD";
                        if (/\b(SD|480p?)\b/i.test(text)) return "SD";
                        return "";
                    };
                    const values = [];
                    document.querySelectorAll(
                        "button, a, [role='button'], [role='option'], li, label, " +
                        "[data-quality], [data-resolution]"
                    ).forEach((el) => {
                        const blob = clean([
                            el.innerText, el.textContent, el.getAttribute("aria-label"),
                            el.getAttribute("title"), el.getAttribute("data-quality"),
                            el.getAttribute("data-resolution"), el.className,
                        ].filter(Boolean).join(" "));
                        const quality = qualityOf(blob);
                        if (quality && !values.includes(quality)) values.push(quality);
                    });
                    return values;
                }"""
            )
            for label in labels or []:
                normalized = normalize_quality_hint(str(label))
                if normalized and normalized not in discovered:
                    discovered.append(normalized)
        except Exception:
            continue

    order = {"4K": 0, "FHD": 1, "HD": 2, "SD": 3}
    discovered.sort(key=lambda value: order.get(value, 99))
    activated: list[str] = []

    for target in discovered[:8]:
        before_items = await collect_dom_stream_candidates(page)
        before_urls = {
            canonicalize_stream_url(url)
            for item in before_items
            for url in extract_stream_urls(item.get("url", ""))
            if canonicalize_stream_url(url)
        }
        clicked = False
        for frame in list(page.frames):
            try:
                clicked = bool(await frame.evaluate(
                    r"""async (target) => {
                        const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                        const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                        const visible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 &&
                                style.display !== "none" && style.visibility !== "hidden";
                        };
                        const qualityOf = (value) => {
                            const text = clean(value);
                            if (/\b(4K|UHD|2160p?)\b/i.test(text)) return "4K";
                            if (/\b(Full\s*HD|FHD|1080p?)\b/i.test(text)) return "FHD";
                            if (/\b(HD|720p?)\b/i.test(text)) return "HD";
                            if (/\b(SD|480p?)\b/i.test(text)) return "SD";
                            return "";
                        };
                        const selector = "button, a, [role='button'], [role='option'], li, label, " +
                            "[data-quality], [data-resolution]";
                        const textOf = (el) => clean([
                            el.innerText, el.textContent, el.getAttribute("aria-label"),
                            el.getAttribute("title"), el.getAttribute("data-quality"),
                            el.getAttribute("data-resolution"), el.className,
                        ].filter(Boolean).join(" "));

                        let nodes = Array.from(document.querySelectorAll(selector));
                        const menu = nodes.find((el) => visible(el) &&
                            /quality|chất lượng|độ phân giải/i.test(textOf(el)));
                        if (menu) {
                            try { menu.click(); await delay(350); } catch (_) {}
                        }
                        nodes = Array.from(document.querySelectorAll(selector));
                        const option = nodes.find((el) => {
                            if (!visible(el) || qualityOf(textOf(el)) !== target) return false;
                            if (el.tagName !== "A") return true;
                            const href = String(el.getAttribute("href") || "").trim();
                            return !href || href === "#" || href.startsWith("javascript:");
                        });
                        if (!option) return false;
                        try {
                            option.scrollIntoView({block: "center", inline: "center"});
                            option.click();
                            option.dispatchEvent(new MouseEvent("click", {
                                bubbles: true, cancelable: true, view: window,
                            }));
                            await delay(550);
                            return true;
                        } catch (_) {
                            return false;
                        }
                    }""",
                    target,
                ))
            except Exception:
                clicked = False

            if clicked:
                await page.wait_for_timeout(1100)
                await stimulate_player(page)
                after_items = await collect_dom_stream_candidates(page)
                new_count = 0
                for candidate in after_items:
                    extracted = extract_stream_urls(candidate.get("url", ""))
                    for raw_stream in extracted:
                        canonical = canonicalize_stream_url(raw_stream)
                        if not canonical or canonical in before_urls:
                            continue
                        capture_callback(
                            canonical,
                            f"quality/{target}",
                            frame_url=candidate.get("frame_url", ""),
                            quality=target or candidate.get("quality", ""),
                        )
                        new_count += 1
                # Network event handlers vẫn bắt nguồn nếu player tái sử dụng URL cũ.
                if new_count == 0:
                    print(
                        f"   ℹ️ Đã bấm {target} nhưng DOM chưa xuất hiện URL mới; "
                        "chờ request/response của player.",
                        flush=True,
                    )
                activated.append(target)
                break

    return activated


async def read_match_metadata(
    page: Page,
    match_url: str,
    match_name: str = "",
    blv_slug: str = "",
) -> dict[str, Any]:
    try:
        data = await page.evaluate(
            r"""({matchName, blvSlug}) => {
                const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                const norm = (v) => clean(v).normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
                const fixtureId = (location.pathname.match(/\/s8-live\/(\d+)/i) || [])[1] || "";
                const streamKey = new URLSearchParams(location.search).get("s8_live_stream_key") ||
                    ((location.pathname.match(/\/s8-live\/\d+\/([^/?#]+)/i) || [])[1] || "");
                const exactIdentityRoots = [];
                const addIdentityRoot = (node) => {
                    if (node && !exactIdentityRoots.includes(node)) exactIdentityRoots.push(node);
                };
                if (fixtureId) {
                    [
                        `[data-fixture-id="${fixtureId}"]`, `[data-match-id="${fixtureId}"]`,
                        `[data-event-id="${fixtureId}"]`, `[data-id="${fixtureId}"]`,
                        `a[href*="/s8-live/${fixtureId}/"]`
                    ].forEach((selector) => document.querySelectorAll(selector).forEach((node) => addIdentityRoot(node)));
                }
                if (streamKey) {
                    document.querySelectorAll(`[data-stream-key="${CSS.escape(streamKey)}"], a[href*="${CSS.escape(streamKey)}"]`)
                        .forEach((node) => addIdentityRoot(node));
                }
                const expandIdentityRoot = (node) => {
                    let current = node;
                    let best = node;
                    for (let depth = 0; current && depth < 8; depth += 1, current = current.parentElement) {
                        const links = Array.from(current.querySelectorAll?.("a[href*='/s8-live/']") || []);
                        const own = links.filter((a) => {
                            const href = a.href || a.getAttribute("href") || "";
                            return (fixtureId && href.includes(`/s8-live/${fixtureId}/`)) || (streamKey && href.includes(streamKey));
                        });
                        if (own.length && links.length <= 1) best = current;
                        if (links.length > 1) break;
                    }
                    return best;
                };
                const exactRoot = exactIdentityRoots.length
                    ? exactIdentityRoots.map(expandIdentityRoot).sort((a, b) => clean(a.innerText).length - clean(b.innerText).length)[0]
                    : null;
                const cleanTeamName = (value) => {
                    let text = clean(value);
                    if (!text || text.length > 110) return "";
                    text = text.replace(/^(?:home|away|đội nhà|đội khách)\s*[:\-]?\s*/i, "");
                    if (!text || /(?:đang diễn ra|sắp diễn ra|\blive\b|trực tiếp|bình luận|tỷ số|kèo|\d{1,2}:\d{2})/i.test(text)) return "";
                    if (/^[0-9\-: ]+$/.test(text)) return "";
                    return text;
                };
                const pairFromScope = (scope) => {
                    if (!scope) return "";
                    const firstValue = (selectors, attrs) => {
                        for (const selector of selectors) {
                            const el = scope.querySelector?.(selector);
                            if (!el) continue;
                            for (const attr of attrs) {
                                const value = cleanTeamName(el.getAttribute?.(attr));
                                if (value) return value;
                            }
                            const value = cleanTeamName(el.innerText || el.textContent || el.getAttribute?.("alt"));
                            if (value) return value;
                        }
                        return "";
                    };
                    const home = firstValue(
                        ["[data-home-team]", "[data-home-name]", "[data-home]", "[class*='home-team']", "[class*='team-home']"],
                        ["data-home-team", "data-home-name", "data-home", "data-name", "data-team-name"]
                    );
                    const away = firstValue(
                        ["[data-away-team]", "[data-away-name]", "[data-away]", "[class*='away-team']", "[class*='team-away']"],
                        ["data-away-team", "data-away-name", "data-away", "data-name", "data-team-name"]
                    );
                    if (home && away && norm(home) !== norm(away)) return `${home} vs ${away}`;

                    const names = [];
                    const unique = new Set();
                    scope.querySelectorAll?.(
                        "[data-team-name], [class*='team-name'], [class*='club-name'], " +
                        "[class*='team'] [class*='name'], [class*='club'] [class*='name'], " +
                        "[class*='team'] img[alt], [class*='club'] img[alt]"
                    ).forEach((el) => {
                        const value = cleanTeamName(
                            el.getAttribute?.("data-team-name") || el.getAttribute?.("data-name") ||
                            el.getAttribute?.("alt") || el.getAttribute?.("title") || el.innerText || el.textContent
                        );
                        const key = norm(value);
                        if (value && !unique.has(key)) { unique.add(key); names.push(value); }
                    });
                    return names.length >= 2 ? `${names[0]} vs ${names[1]}` : "";
                };
                const discoverTitle = () => {
                    const exactPair = pairFromScope(exactRoot);
                    if (exactPair) return exactPair;
                    const exactLines = String(exactRoot?.innerText || exactRoot?.textContent || "")
                        .split(/[\n|•]+/).map(clean).filter(Boolean);
                    const exactLine = exactLines.find((value) => /\bvs\b/i.test(value) && value.length <= 220);
                    if (exactLine) return exactLine;
                    const candidates = [
                        document.querySelector("meta[property='og:title']")?.content,
                        document.querySelector("meta[name='twitter:title']")?.content,
                        document.querySelector("meta[itemprop='name']")?.content,
                        document.querySelector("h1")?.innerText,
                        document.querySelector("[class*='match-title']")?.innerText,
                        document.querySelector("[class*='match-name']")?.innerText,
                        document.querySelector("[class*='event-title']")?.innerText,
                        document.title
                    ].map(clean).filter(Boolean);
                    const direct = candidates.find((value) => /\bvs\b/i.test(value) && value.length <= 240);
                    if (direct) return direct;
                    const bodyLines = String(document.body?.innerText || "")
                        .split(/[\n|•]+/).map(clean).filter(Boolean);
                    const line = bodyLines.find((value) => /\bvs\b/i.test(value) && value.length <= 220);
                    if (line) return line;
                    return pairFromScope(document) || clean(matchName);
                };
                let title = discoverTitle();
                const teamParts = clean(title || matchName).split(/\s+vs\s+/i);
                const homeTokens = norm(teamParts[0] || "").split(/[^a-z0-9]+/).filter((v) => v.length >= 4);
                const awayTokens = norm((teamParts[1] || "").split(" - ")[0]).split(/[^a-z0-9]+/).filter((v) => v.length >= 4);
                const logoItems = [];
                const seen = new Set();

                function logoValue(input, depth = 0) {
                    if (depth > 6 || input == null) return "";
                    if (typeof input === "string" || typeof input === "number") {
                        const value = String(input).trim();
                        return /^\[object\s+object\]$/i.test(value) ? "" : value;
                    }
                    if (Array.isArray(input)) {
                        for (const item of input) {
                            const value = logoValue(item, depth + 1);
                            if (value) return value;
                        }
                        return "";
                    }
                    if (typeof input === "object") {
                        for (const key of ["url", "contentUrl", "content_url", "src", "href", "@id", "value", "image", "logo"]) {
                            if (Object.prototype.hasOwnProperty.call(input, key)) {
                                const value = logoValue(input[key], depth + 1);
                                if (value) return value;
                            }
                        }
                    }
                    return "";
                }

                function addLogo(v, score = 0, context = "", source = "detail-match") {
                    let value = logoValue(v);
                    if (!value || value.startsWith("data:") || value.startsWith("blob:") || /\[object\s+object\]/i.test(value)) return;
                    try { value = new URL(value, location.href).href; } catch (_) {}
                    if (seen.has(value)) return;
                    seen.add(value);
                    const normalizedContext = norm(`${context} ${value}`);
                    if (homeTokens.some((token) => normalizedContext.includes(token))) score += 60;
                    else if (awayTokens.some((token) => normalizedContext.includes(token))) score += 35;
                    if (/team|club|home|away|doi|đội/i.test(context)) score += 12;
                    if (/avatar|blv|comment|banner|advert|flag|league/i.test(context)) score -= 35;
                    logoItems.push({url: value, score, context: clean(context), source});
                }

                function inspectImage(img, baseScore = 0, source = "detail-match") {
                    const nearby = clean([
                        img.alt, img.title, img.className,
                        img.parentElement?.innerText, img.parentElement?.className,
                        img.parentElement?.parentElement?.innerText,
                        img.parentElement?.parentElement?.className
                    ].filter(Boolean).join(" ")).slice(0, 500);
                    let score = baseScore;
                    const width = img.naturalWidth || img.width || 0;
                    const height = img.naturalHeight || img.height || 0;
                    if (width && height && Math.abs(width - height) <= Math.max(width, height) * 0.35) score += 5;
                    [
                        img.currentSrc, img.src, img.getAttribute("src"),
                        img.getAttribute("data-src"), img.getAttribute("data-original"),
                        img.getAttribute("data-lazy-src")
                    ].forEach((value) => addLogo(value, score, nearby, source));
                    [img.getAttribute("srcset"), img.getAttribute("data-srcset")].filter(Boolean)
                        .forEach((set) => set.split(",").forEach((part) =>
                            addLogo(part.trim().split(/\s+/)[0], score, nearby, source)
                        ));
                }

                const tokenMatchCount = (value, tokens) => {
                    const text = norm(value);
                    return tokens.filter((token) => text.includes(token)).length;
                };
                const belongsToCurrentMatch = (node) => {
                    const blob = clean([
                        node?.innerText, node?.textContent, node?.className,
                        node?.getAttribute?.("aria-label"), node?.getAttribute?.("data-team"),
                        node?.getAttribute?.("data-home"), node?.getAttribute?.("data-away")
                    ].filter(Boolean).join(" ")).slice(0, 1600);
                    return tokenMatchCount(blob, homeTokens) > 0 || tokenMatchCount(blob, awayTokens) > 0;
                };

                const rootSelectors = [
                    "[class*='match-info']", "[class*='match-detail']", "[class*='event-detail']",
                    "[class*='match-header']", "[class*='fixture-detail']", "article", "main"
                ];
                const rootCandidates = Array.from(document.querySelectorAll(rootSelectors.join(",")))
                    .filter((node) => {
                        const blob = clean(node.innerText || node.textContent || "").slice(0, 5000);
                        return tokenMatchCount(blob, homeTokens) > 0 && tokenMatchCount(blob, awayTokens) > 0;
                    })
                    .sort((a, b) => clean(a.innerText || a.textContent).length - clean(b.innerText || b.textContent).length);
                const primaryRoot = exactRoot || rootCandidates[0] || document.querySelector("[class*='match-detail']") || null;

                const teamScope = primaryRoot || document;
                const teamNodes = Array.from(teamScope.querySelectorAll(
                    "[class*='team'], [class*='club'], [class*='home'], [class*='away'], " +
                    "[data-team], [data-home], [data-away]"
                )).filter(belongsToCurrentMatch);
                teamNodes.forEach((node) =>
                    node.querySelectorAll("img").forEach((img) => inspectImage(img, 24, "detail-team"))
                );

                if (primaryRoot) {
                    primaryRoot.querySelectorAll("img").forEach((img) => {
                        const nearby = clean([
                            img.alt, img.title, img.parentElement?.innerText,
                            img.parentElement?.parentElement?.innerText
                        ].filter(Boolean).join(" "));
                        if (belongsToCurrentMatch(img.parentElement) || tokenMatchCount(nearby, homeTokens) || tokenMatchCount(nearby, awayTokens)) {
                            inspectImage(img, 10, "detail-match");
                        }
                    });
                }

                [
                    document.querySelector("meta[property='og:image']")?.content,
                    document.querySelector("meta[name='twitter:image']")?.content,
                    document.querySelector("link[rel='image_src']")?.href
                ].forEach((value) => addLogo(value, -15, "meta image", "meta"));

                const titleSelectors = [
                    "h1", "[class*='match-title']", "[class*='match-name']",
                    "[class*='event-title']", "h2", "title"
                ];
                let titleNode = null;
                for (const selector of titleSelectors) {
                    const nodes = selector === "title" ? [document.querySelector("title")] : Array.from(document.querySelectorAll(selector));
                    const found = nodes.find((el) => el && /\bvs\b/i.test(clean(el.innerText || el.textContent)));
                    if (found) {
                        const foundTitle = clean(found.innerText || found.textContent);
                        if (foundTitle) title = foundTitle;
                        titleNode = found;
                        break;
                    }
                }

                // Thu hẹp thêm từ tiêu đề trận để không vô tình dùng giờ của trận liên quan bên dưới.
                let timeRoot = primaryRoot;
                if (titleNode && titleNode.tagName !== "TITLE") {
                    let node = titleNode;
                    let best = titleNode.parentElement || titleNode;
                    for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
                        const blob = clean(node.innerText || node.textContent || "").slice(0, 4000);
                        const links = Array.from(node.querySelectorAll?.("a[href*='/s8-live/']") || []);
                        if (tokenMatchCount(blob, homeTokens) > 0 && tokenMatchCount(blob, awayTokens) > 0 && links.length <= 1) {
                            best = node;
                        } else if (links.length > 1) {
                            break;
                        }
                    }
                    timeRoot = best;
                }

                const timeCandidates = [];
                const timeSeen = new Set();
                const looksLikeTime = (value) =>
                    /(?:^|\D)(?:[01]?\d|2[0-3])[:h.]?[0-5]\d(?:\D|$)/i.test(String(value || "")) ||
                    /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(String(value || ""));
                const addTime = (value, score, source) => {
                    const fixed = clean(value);
                    if (!fixed || !looksLikeTime(fixed) || fixed.length > 500) return;
                    const key = `${fixed}\n${source}`;
                    if (timeSeen.has(key)) return;
                    timeSeen.add(key);
                    timeCandidates.push({value: fixed, score, source});
                };
                const nodeBelongsToMatch = (element) => {
                    let node = element;
                    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
                        if (belongsToCurrentMatch(node)) return true;
                    }
                    return false;
                };
                const scanTimeScope = (scope, baseScore, source, strictContext = false) => {
                    if (!scope) return;
                    scope.querySelectorAll(
                        "time, [datetime], [data-time], [data-start], [data-date], [data-kickoff], " +
                        "[data-start-time], [data-match-time], [data-event-time], " +
                        "[class*='kickoff'], [class*='match-time'], [class*='event-time'], " +
                        "[class*='start-time'], [class*='match-date'], [class*='event-date']"
                    ).forEach((el) => {
                        if (strictContext && !nodeBelongsToMatch(el)) return;
                        addTime(el.getAttribute("datetime"), baseScore + 25, `${source}/datetime`);
                        addTime(el.getAttribute("data-start"), baseScore + 22, `${source}/data-start`);
                        addTime(el.getAttribute("data-time"), baseScore + 20, `${source}/data-time`);
                        addTime(el.getAttribute("data-date"), baseScore + 15, `${source}/data-date`);
                        addTime(el.getAttribute("data-kickoff"), baseScore + 24, `${source}/data-kickoff`);
                        addTime(el.getAttribute("data-start-time"), baseScore + 23, `${source}/data-start-time`);
                        addTime(el.getAttribute("data-match-time"), baseScore + 23, `${source}/data-match-time`);
                        addTime(el.getAttribute("data-event-time"), baseScore + 23, `${source}/data-event-time`);
                        addTime(el.innerText || el.textContent, baseScore + 10, `${source}/visible`);
                    });
                    const matchLinks = Array.from(scope.querySelectorAll?.("a[href*='/s8-live/']") || []);
                    const broadRoot = scope === document || scope.tagName === "MAIN" || matchLinks.length > 1;
                    if (!broadRoot) addTime(scope.innerText || scope.textContent, baseScore, `${source}/root-text`);
                };

                // Chỉ ưu tiên khối nhỏ nhất có đúng hai đội; khối rộng phải qua kiểm tra ngữ cảnh.
                const timeRootLinks = Array.from(timeRoot?.querySelectorAll?.("a[href*='/s8-live/']") || []);
                const timeRootIsBroad = !timeRoot || timeRoot.tagName === "MAIN" || timeRootLinks.length > 1;
                scanTimeScope(timeRoot, 100, "match-root", timeRootIsBroad);

                document.querySelectorAll("script[type='application/ld+json']").forEach((script) => {
                    try {
                        const raw = JSON.parse(script.textContent || "null");
                        const visited = new Set();
                        const walk = (item, depth = 0) => {
                            if (!item || depth > 8 || typeof item !== "object" || visited.has(item)) return;
                            visited.add(item);
                            if (Array.isArray(item)) { item.forEach((value) => walk(value, depth + 1)); return; }
                            const itemName = clean(item.name || item.headline || item.title || "");
                            const homeName = cleanTeamName(item.homeTeam?.name || item.homeTeam || item.home?.name || item.home || "");
                            const awayName = cleanTeamName(item.awayTeam?.name || item.awayTeam || item.away?.name || item.away || "");
                            const eventTitle = /\bvs\b/i.test(itemName) ? itemName :
                                (homeName && awayName ? `${homeName} vs ${awayName}` : "");
                            if (eventTitle && (!title || !/\bvs\b/i.test(title))) title = eventTitle;
                            const itemType = clean(item["@type"] || "");
                            const eventMatches = eventTitle ? (
                                !title || norm(eventTitle) === norm(title) ||
                                (homeTokens.some((token) => norm(eventTitle).includes(token)) &&
                                 awayTokens.some((token) => norm(eventTitle).includes(token)))
                            ) : /(?:SportsEvent|Event)/i.test(itemType);
                            if (item.startDate && eventMatches) addTime(String(item.startDate), 90, "json-ld/startDate");
                            if (item.startTime && eventMatches) addTime(String(item.startTime), 88, "json-ld/startTime");
                            if (item.date && eventMatches) addTime(String(item.date), 65, "json-ld/date");
                            if (item.image) (Array.isArray(item.image) ? item.image : [item.image])
                                .forEach((value) => addLogo(value?.url || value, 2, itemName || "json ld"));
                            Object.values(item).forEach((value) => walk(value, depth + 1));
                        };
                        walk(raw);
                    } catch (_) {}
                });


                // Nhiều trang s8-live giữ lịch trong biến JS thay vì DOM/JSON-LD.
                // Chỉ đọc script có đúng fixture_id hoặc stream_key để tránh lấy giờ trận bên cạnh.
                const relatedScriptText = Array.from(document.scripts)
                    .map((script) => script.textContent || "")
                    .filter((text) => (fixtureId && text.includes(fixtureId)) || (streamKey && text.includes(streamKey)))
                    .join("\n").slice(0, 1200000);
                if (relatedScriptText) {
                    const addEpoch = (rawValue, score, source) => {
                        const digits = String(rawValue || "").replace(/[^0-9]/g, "");
                        if (!/^\d{10,13}$/.test(digits)) return;
                        const numeric = Number(digits);
                        if (!Number.isFinite(numeric)) return;
                        const millis = digits.length === 10 ? numeric * 1000 : numeric;
                        const date = new Date(millis);
                        if (!Number.isNaN(date.getTime())) addTime(date.toISOString(), score, source);
                    };
                    const keyedStringPatterns = [
                        /["'](?:startDate|start_date|startTime|start_time|kickoff|kick_off|matchTime|match_time|eventTime|event_time|fixtureTime|fixture_time|scheduledAt|scheduled_at|startAt|start_at)["']\s*[:=]\s*["']([^"']{4,120})["']/gi,
                        /(?:startDate|start_date|startTime|start_time|kickoff|kick_off|matchTime|match_time|eventTime|event_time|fixtureTime|fixture_time|scheduledAt|scheduled_at|startAt|start_at)\s*[:=]\s*`([^`]{4,120})`/gi,
                    ];
                    keyedStringPatterns.forEach((pattern) => {
                        let found;
                        while ((found = pattern.exec(relatedScriptText)) !== null) {
                            addTime(found[1], 96, "fixture-script/string");
                        }
                    });
                    const epochPattern = /["'](?:timestamp|start_timestamp|startTimestamp|kickoff_timestamp|kickoffTimestamp|match_timestamp|matchTimestamp|start_time|startTime)["']\s*[:=]\s*["']?(\d{10,13})["']?/gi;
                    let epochFound;
                    while ((epochFound = epochPattern.exec(relatedScriptText)) !== null) {
                        addEpoch(epochFound[1], 94, "fixture-script/epoch");
                    }
                }

                // Chỉ dùng selector toàn trang làm fallback khi khối trận không cho ra giờ nào.
                if (!timeCandidates.length) scanTimeScope(document, 10, "document-fallback", true);
                timeCandidates.sort((a, b) => b.score - a.score);

                const iframeUrls = Array.from(document.querySelectorAll("iframe[src]"))
                    .map((el) => el.src || el.getAttribute("src") || "").filter(Boolean);

                const qualitySources = [];
                const qualitySeen = new Set();
                const qualityOf = (value) => {
                    const text = clean(value);
                    const match = text.match(/\b(4K|UHD|2160p?|Full\s*HD|FHD|1080p?|HD|720p?|SD|480p?)\b/i);
                    return match ? match[1] : "";
                };
                const addQualitySource = (value, quality, context = "") => {
                    if (!value) return;
                    const raw = String(value).trim();
                    if (!raw || raw.length > 12000) return;
                    const key = `${raw}\n${quality}`;
                    if (qualitySeen.has(key)) return;
                    qualitySeen.add(key);
                    qualitySources.push({url: raw, quality: qualityOf(quality || context), context: clean(context)});
                };
                document.querySelectorAll(
                    "iframe[src], a[href], source[src], video[src], [data-url], [data-src], " +
                    "[data-stream], [data-stream-url], [data-quality], [data-resolution], [data-file]"
                ).forEach((el) => {
                    const attrs = [
                        el.getAttribute("src"), el.getAttribute("href"), el.getAttribute("data-url"),
                        el.getAttribute("data-src"), el.getAttribute("data-stream"),
                        el.getAttribute("data-stream-url"), el.getAttribute("data-file")
                    ].filter(Boolean);
                    const context = clean([
                        el.innerText, el.textContent, el.getAttribute("title"), el.getAttribute("aria-label"),
                        el.getAttribute("data-quality"), el.getAttribute("data-resolution"), el.className
                    ].filter(Boolean).join(" "));
                    const quality = qualityOf(context);
                    attrs.forEach((value) => {
                        if (/m3u8|\.flv|streamUrl|stream_url|playurl|source=/i.test(String(value))) {
                            addQualitySource(value, quality, context);
                        }
                    });
                });

                let blv = "";
                const currentSlug = clean(blvSlug || new URLSearchParams(location.search).get("blv") || "").toLowerCase();
                const blvSelectors = [
                    "[data-blv].active", "[data-blv][aria-selected='true']",
                    "[class*='blv'].active", "[class*='commentator'].active",
                    "[class*='blv-name']", "[class*='commentator-name']"
                ];
                for (const selector of blvSelectors) {
                    const el = exactRoot?.querySelector?.(selector) || document.querySelector(selector);
                    const value = clean(el?.innerText || el?.textContent || el?.getAttribute?.("data-name"));
                    if (value && value.length <= 80) { blv = value; break; }
                }
                if (!blv && currentSlug) {
                    const nodes = Array.from(document.querySelectorAll("a[href], [data-blv], [data-commentator]"));
                    const found = nodes.find((el) => {
                        const blob = norm([el.getAttribute("href"), el.getAttribute("data-blv"),
                            el.getAttribute("data-commentator"), el.id, el.className].filter(Boolean).join(" "));
                        return blob.includes(norm(currentSlug));
                    });
                    const value = clean(found?.innerText || found?.textContent || found?.getAttribute?.("data-name"));
                    if (value && value.length <= 80) blv = value;
                }
                if (!blv) {
                    const relatedScripts = Array.from(document.scripts)
                        .map((script) => script.textContent || "")
                        .filter((text) => (fixtureId && text.includes(fixtureId)) || (streamKey && text.includes(streamKey)))
                        .join("\n").slice(0, 800000);
                    const patterns = [
                        /["'](?:blv_name|blvName|blv|commentator_name|commentatorName|commentator)["']\s*:\s*["']([^"']{2,100})["']/i,
                        /(?:BLV|Bình luận viên)\s*[:\-–—]?\s*([^|•\n<]{2,80})/i
                    ];
                    for (const pattern of patterns) {
                        const found = relatedScripts.match(pattern);
                        if (found) { blv = clean(found[1]); break; }
                    }
                }
                if (!blv) {
                    const bodyText = String(document.body?.innerText || "");
                    const match = bodyText.match(/(?:BLV|Bình luận viên)\s*[:\-–—]?\s*([^|•\n]{2,80})/i) ||
                        bodyText.match(/(NGƯỜI\s+[^\n|•]{2,50})\s+TRẬN\b/i);
                    if (match) blv = clean(match[1]);
                }

                const sportParts = [
                    document.body?.getAttribute("data-sport"), document.body?.getAttribute("data-category"),
                    document.querySelector("meta[name='description']")?.content,
                    document.querySelector("[data-sport]")?.getAttribute("data-sport"),
                    document.querySelector("[data-category]")?.getAttribute("data-category"),
                    document.querySelector("[class*='breadcrumb']")?.innerText,
                    document.querySelector("[class*='sport-name']")?.innerText,
                    document.querySelector("[class*='category-name']")?.innerText,
                    document.querySelector("[class*='league-name']")?.innerText, title
                ].filter(Boolean).map(clean);

                logoItems.sort((a, b) => b.score - a.score);
                return {
                    title, time_text: timeCandidates[0]?.value || "",
                    time_candidates: timeCandidates.slice(0, 24),
                    logos: logoItems.map((item) => item.url),
                    logo_candidates: logoItems.slice(0, 24),
                    iframe_urls: iframeUrls,
                    quality_sources: qualitySources.slice(0, 80),
                    sport_text: sportParts.join(" | "), blv
                };
            }""",
            {"matchName": match_name, "blvSlug": blv_slug},
        )
        data["logos"] = [
            url for value in data.get("logos", [])
            if (url := normalize_logo_url(value, match_url))
        ]
        cleaned_candidates = []
        for item in data.get("logo_candidates", []) or []:
            if not isinstance(item, dict):
                item = {"url": item}
            fixed = dict(item)
            fixed["url"] = normalize_logo_url(item, match_url)
            if fixed["url"]:
                cleaned_candidates.append(fixed)
        data["logo_candidates"] = cleaned_candidates
        return data
    except Exception:
        return {
            "title": "", "time_text": "", "time_candidates": [], "logos": [], "logo_candidates": [],
            "iframe_urls": [], "quality_sources": [], "sport_text": "", "blv": "",
        }



async def read_exact_fixture_script_metadata(
    page: Page,
    matches: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Đọc metadata từ cửa sổ script gắn đúng fixture_id/stream_key.

    Trang s8-live có thể nhúng toàn bộ lịch vào một script chung. Quét regex trên
    cả script dễ lấy giờ/tên của trận bên cạnh; hàm này chỉ xét các cửa sổ nhỏ
    quanh đúng fixture hoặc stream key rồi trả metadata theo identity ổn định.
    """
    targets = []
    for match in matches:
        url = clean_text(str(match.get("url", "")))
        if not url:
            continue
        targets.append({
            "identity": gavang_match_identity(url),
            "fixture_id": match_id_from_url(url),
            "stream_key": extract_gavang_stream_key(url),
            "url": url,
        })
    if not targets:
        return {}
    try:
        payload = await page.evaluate(
            r"""({targets}) => {
                const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();
                const scripts = Array.from(document.scripts)
                    .map((script) => script.textContent || "")
                    .filter(Boolean);
                const unescapeValue = (value) => {
                    let text = clean(value);
                    if (!text) return "";
                    try {
                        const escaped = text.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
                        text = JSON.parse(`"${escaped}"`);
                    } catch (_) {}
                    return clean(String(text).replace(/\\\//g, "/"));
                };
                const allMatches = (text, patterns, limit = 12) => {
                    const out = [];
                    for (const pattern of patterns) {
                        pattern.lastIndex = 0;
                        let found;
                        while ((found = pattern.exec(text)) !== null && out.length < limit) {
                            const value = unescapeValue(found[1] || found[2] || "");
                            if (value && !out.includes(value)) out.push(value);
                        }
                    }
                    return out;
                };
                const identityWindows = (target) => {
                    const windows = [];
                    const seen = new Set();
                    for (const script of scripts) {
                        const needles = [target.stream_key, target.fixture_id].filter(Boolean);
                        for (const needle of needles) {
                            let from = 0;
                            let hits = 0;
                            while (hits < 10) {
                                const index = script.indexOf(needle, from);
                                if (index < 0) break;
                                from = index + needle.length;
                                hits += 1;
                                // Cửa sổ nhỏ quanh đúng identity để tránh lấy lịch của fixture kế bên.
                                const start = Math.max(0, index - 3500);
                                const end = Math.min(script.length, index + needle.length + 3500);
                                const windowText = script.slice(start, end);
                                const center = index - start;
                                const marker = `${start}:${end}:${needle}:${windowText.slice(Math.max(0, center - 40), center + 80)}`;
                                if (!seen.has(marker)) {
                                    seen.add(marker);
                                    windows.push({text: windowText, center, needle});
                                }
                            }
                        }
                    }
                    return windows.slice(0, 20);
                };
                const nearestValues = (windows, patterns, limit = 12) => {
                    const ranked = [];
                    for (const window of windows) {
                        for (const pattern of patterns) {
                            pattern.lastIndex = 0;
                            let found;
                            while ((found = pattern.exec(window.text)) !== null) {
                                const value = unescapeValue(found[1] || found[2] || found[0] || "");
                                if (!value) continue;
                                ranked.push({value, distance: Math.abs(found.index - window.center)});
                                if (found[0] === "") pattern.lastIndex += 1;
                            }
                        }
                    }
                    ranked.sort((a, b) => a.distance - b.distance || b.value.length - a.value.length);
                    const output = [];
                    for (const item of ranked) {
                        if (!output.includes(item.value)) output.push(item.value);
                        if (output.length >= limit) break;
                    }
                    return output;
                };
                const result = {};
                for (const target of targets) {
                    const windows = identityWindows(target);
                    const combined = windows.map((item) => item.text).join("\n");
                    const row = {title: "", home: "", away: "", league: "", blv: "", time_candidates: [], logos: [], source: "exact-fixture-script"};
                    if (!combined) { result[target.identity] = row; continue; }

                    const homePatterns = [
                        /["'](?:home_name|homeName|home_team_name|homeTeamName|team_home|homeTeam|home)["']\s*[:=]\s*["']([^"']{2,140})["']/gi,
                    ];
                    const awayPatterns = [
                        /["'](?:away_name|awayName|away_team_name|awayTeamName|team_away|awayTeam|away)["']\s*[:=]\s*["']([^"']{2,140})["']/gi,
                    ];
                    const titlePatterns = [
                        /["'](?:match_name|matchName|event_name|eventName|fixture_name|fixtureName|title)["']\s*[:=]\s*["']([^"']{4,240})["']/gi,
                    ];
                    const leaguePatterns = [
                        /["'](?:league_name|leagueName|competition_name|competitionName|tournament_name|tournamentName)["']\s*[:=]\s*["']([^"']{2,140})["']/gi,
                    ];
                    const blvPatterns = [
                        /["'](?:blv_name|blvName|blv|commentator_name|commentatorName|commentator)["']\s*[:=]\s*["']([^"']{2,100})["']/gi,
                    ];
                    row.home = nearestValues(windows, homePatterns, 1)[0] || "";
                    row.away = nearestValues(windows, awayPatterns, 1)[0] || "";
                    row.league = nearestValues(windows, leaguePatterns, 1)[0] || "";
                    row.blv = nearestValues(windows, blvPatterns, 1)[0] || "";
                    const titleCandidates = nearestValues(windows, titlePatterns, 8)
                        .filter((value) => /\bvs\b/i.test(value) && value.length <= 240);
                    if (row.home && row.away && row.home.toLowerCase() !== row.away.toLowerCase()) {
                        row.title = `${row.league ? `${row.league} - ` : ""}${row.home} VS ${row.away}`;
                    } else {
                        row.title = titleCandidates[0] || "";
                    }

                    const timePatterns = [
                        /["'](?:startDate|start_date|startTime|start_time|kickoff|kick_off|matchTime|match_time|eventTime|event_time|fixtureTime|fixture_time|fixture_start|fixtureStart|scheduledAt|scheduled_at|startAt|start_at|datetime|date_time|dateTime|s8_live_time|s8LiveTime|s8_live_start|s8LiveStart)["']\s*[:=]\s*["']([^"']{4,120})["']/gi,
                        /["'](?:start_timestamp|startTimestamp|kickoff_timestamp|kickoffTimestamp|fixture_timestamp|fixtureTimestamp|event_timestamp|eventTimestamp|s8_live_timestamp|s8LiveTimestamp)["']\s*[:=]\s*["']?(\d{10,13})["']?/gi,
                    ];
                    const datePatterns = [
                        /["'](?:match_date|matchDate|event_date|eventDate|fixture_date|fixtureDate|start_date|startDateOnly|s8_live_date|s8LiveDate|date)["']\s*[:=]\s*["']([^"']{4,40})["']/gi,
                    ];
                    const clockPatterns = [
                        /["'](?:match_clock|matchClock|kickoff_time|kickoffTime|fixture_clock|fixtureClock|start_clock|startClock|s8_live_clock|s8LiveClock|time)["']\s*[:=]\s*["']((?:[01]?\d|2[0-3]):[0-5]\d)["']/gi,
                    ];
                    const values = nearestValues(windows, timePatterns, 18);
                    const dates = nearestValues(windows, datePatterns, 6);
                    const clocks = nearestValues(windows, clockPatterns, 6);
                    for (const value of values) row.time_candidates.push({value, score: 132, source: "exact-fixture-script/keyed"});
                    if (dates.length && clocks.length) {
                        row.time_candidates.push({value: `${dates[0]} ${clocks[0]}`, score: 134, source: "exact-fixture-script/date+time"});
                    }
                    const iso = combined.match(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?/g) || [];
                    iso.slice(0, 8).forEach((value) => row.time_candidates.push({value, score: 112, source: "exact-fixture-script/iso"}));
                    const epochPattern = /["'](?:timestamp|start_timestamp|startTimestamp|kickoff_timestamp|kickoffTimestamp|match_timestamp|matchTimestamp)["']\s*[:=]\s*["']?(\d{10,13})["']?/gi;
                    let epoch;
                    while ((epoch = epochPattern.exec(combined)) !== null && row.time_candidates.length < 28) {
                        const raw = epoch[1];
                        const number = Number(raw);
                        const millis = raw.length === 10 ? number * 1000 : number;
                        const date = new Date(millis);
                        if (!Number.isNaN(date.getTime())) row.time_candidates.push({value: date.toISOString(), score: 130, source: "exact-fixture-script/epoch"});
                    }

                    const logoPatterns = [
                        /["'](?:home_logo|homeLogo|away_logo|awayLogo|team_logo|teamLogo|logo|image|image_url|imageUrl)["']\s*[:=]\s*["']([^"']{4,500})["']/gi,
                    ];
                    const logoValues = nearestValues(windows, logoPatterns, 16);
                    const logoSeen = new Set();
                    for (const value of logoValues) {
                        try {
                            const url = new URL(value, location.href).href;
                            if (/^https?:\/\//i.test(url) && !logoSeen.has(url)) { logoSeen.add(url); row.logos.push(url); }
                        } catch (_) {}
                    }
                    result[target.identity] = row;
                }
                return result;
            }""",
            {"targets": targets},
        )
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def merge_exact_fixture_script_metadata(
    metadata: dict[str, Any],
    exact: dict[str, Any] | None,
    match_url: str,
) -> dict[str, Any]:
    """Gộp metadata exact-fixture vào kết quả DOM mà không nhận dữ liệu chéo."""
    if not isinstance(exact, dict):
        return metadata
    merged = dict(metadata)
    exact_title = clean_text(str(exact.get("title", "")))
    if exact_title:
        fixed = clean_match_name(exact_title, match_url)
        confidence = title_stream_key_confidence(fixed, match_url)
        if not confidence["contradictory"] and re.search(r"\bvs\b", fixed, re.I):
            current = clean_text(str(merged.get("title", "")))
            if not current or _match_title_score(fixed) > _match_title_score(current):
                merged["title"] = fixed
    exact_times = [item for item in exact.get("time_candidates", []) or [] if isinstance(item, dict)]
    if exact_times:
        current_times = [item for item in merged.get("time_candidates", []) or []]
        merged["time_candidates"] = exact_times + current_times
        merged["time_text"] = clean_text(str(exact_times[0].get("value", ""))) or clean_text(str(merged.get("time_text", "")))
    exact_blv = normalize_blv_name(clean_text(str(exact.get("blv", ""))))
    if exact_blv:
        merged["blv"] = exact_blv
    logos = [normalize_logo_url(value, match_url) for value in exact.get("logos", []) or []]
    logos = [value for value in logos if value]
    if logos:
        merged["logos"] = logos + [value for value in merged.get("logos", []) or [] if value not in logos]
        exact_candidates = [{"url": value, "score": 140, "context": "exact fixture script", "source": "exact-fixture-script"} for value in logos]
        merged["logo_candidates"] = exact_candidates + list(merged.get("logo_candidates", []) or [])
    merged["exact_fixture_script_found"] = bool(exact_title or exact_times or exact_blv or logos)
    return merged

def apply_basic_match_metadata(
    match: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, str]:
    """Áp dụng tên, giờ và BLV từ trang trận vào record hiện tại."""
    changes: dict[str, str] = {}

    metadata_title_safe = True
    if metadata.get("title"):
        better_name = clean_match_name(str(metadata["title"]), str(match.get("url", "")))
        title_confidence = title_stream_key_confidence(better_name, str(match.get("url", "")))
        match["detail_title_key_confidence"] = title_confidence
        if title_confidence["contradictory"]:
            metadata_title_safe = False
            match.setdefault("metadata_warnings", []).append(
                "detail-page: tên trang không khớp stream_key; giữ stream và tên an toàn, không nhận metadata chéo"
            )
            fallback = fallback_match_name_from_stream_key(str(match.get("url", "")))
            if fallback and title_stream_key_confidence(str(match.get("match_name", "")), str(match.get("url", "")))["contradictory"]:
                match["match_name"] = fallback
                changes["match_name"] = fallback
        elif re.search(r"\bvs\b", better_name, re.I):
            title_date = extract_date(better_name)
            # Tiêu đề Gà Vàng thường kết thúc bằng ``- 22-07``. Đưa ngày vào
            # metadata riêng để M3U hiển thị [22/07], tránh lặp ngày trong tên đội.
            better_name = re.sub(
                r"\s*-\s*(?:0?[1-9]|[12]\d|3[01])[-/](?:0?[1-9]|1[0-2])(?:[-/]\d{2,4})?\s*$",
                "",
                better_name,
            ).strip(" -")
            old_name = clean_text(str(match.get("match_name", "")))
            if better_name != old_name:
                match["match_name"] = better_name
                changes["match_name"] = better_name
            if title_date and not clean_text(str(match.get("date", ""))):
                match["date"] = title_date
                match["date_source"] = "detail-title"
                changes["date"] = title_date

    match["detail_metadata_safe"] = metadata_title_safe
    detail_time, detail_date, detail_time_source = select_best_time_candidate(metadata)
    if detail_time and metadata_title_safe:
        old_time = clean_text(str(match.get("time", "")))
        match["time"] = detail_time
        match["date"] = detail_date or clean_text(str(match.get("date", "")))
        match["time_source"] = detail_time_source
        if detail_time != old_time:
            changes["time"] = detail_time
        if detail_date:
            changes["date"] = detail_date

    raw_blv = clean_text(str(metadata.get("blv", "")))
    # Nếu tiêu đề trang rõ ràng thuộc fixture khác thì BLV trên cùng DOM cũng bị coi là không an toàn.
    if raw_blv and metadata_title_safe:
        fixed_blv = normalize_blv_name(raw_blv)
        if fixed_blv and fixed_blv != clean_text(str(match.get("blv", ""))):
            match["blv"] = fixed_blv
            changes["blv"] = fixed_blv

    annotate_match_timing(match)
    return changes


def merge_metadata_logos(match: dict[str, Any], metadata: dict[str, Any]) -> None:
    if match.get("detail_metadata_safe") is False:
        return
    logo_candidates: list[Any] = list(match.get("logo_candidates") or [])
    logo_candidates.extend(match.get("team_logos") or [])
    if match.get("logo"):
        logo_candidates.append(match["logo"])
    logo_candidates.extend(metadata.get("logo_candidates") or [])
    logo_candidates.extend(metadata.get("logos") or [])
    match["logo_candidates"] = logo_candidates
    match["team_logos"] = [
        item.get("url", "") if isinstance(item, dict) else absolute_url(str(item), str(match.get("url", "")))
        for item in logo_candidates if item
    ]
    match["logo"] = choose_logo(
        logo_candidates, str(match.get("url", "")), str(match.get("match_name", ""))
    )


async def enrich_verified_match_metadata(
    context: BrowserContext,
    match: dict[str, Any],
) -> None:
    """Mở trang trận ở chế độ chỉ lấy metadata, không chạy player.

    Áp dụng cho cả FLV đã xác minh và FLV pending. Route filter chặn media nên
    bước này chỉ đọc tên đội, lịch, BLV và logo đúng fixture; không tải luồng video.
    """
    page: Page | None = None
    try:
        page = await context.new_page()
        # Chặn media để trang không tự tải FLV/HLS; script/DOM metadata vẫn hoạt động.
        await install_route_filter(page, homepage=True)
        await page.goto(str(match.get("url", "")), wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1200)
        blv_slug = (parse_qs(urlparse(str(match.get("url", ""))).query).get("blv") or [""])[0]
        metadata = await read_match_metadata(
            page, str(match.get("url", "")), str(match.get("match_name", "")), blv_slug
        )
        exact_map = await read_exact_fixture_script_metadata(page, [match])
        metadata = merge_exact_fixture_script_metadata(
            metadata,
            exact_map.get(gavang_match_identity(str(match.get("url", "")))),
            str(match.get("url", "")),
        )
        changes = apply_basic_match_metadata(match, metadata)
        match["sport_group"] = classify_sport(
            match.get("sport_hint", ""),
            metadata.get("sport_text", ""),
            match.get("card_text", ""),
            match.get("match_name", ""),
            match.get("url", ""),
            default=match.get("sport_group", "Bóng đá"),
        )
        merge_metadata_logos(match, metadata)
        match["metadata_enriched"] = True
        parts = [
            f"tên={match.get('match_name', '')}",
            f"giờ={' '.join(v for v in (match.get('time', ''), match.get('date', '')) if v) or 'không rõ'}",
            f"BLV={match.get('blv') or 'không rõ'}",
        ]
        print(f"   🧾 Bổ sung metadata Gà Vàng: {' | '.join(parts)}", flush=True)
        if changes.get("time") and match.get("kickoff_iso"):
            print(
                f"   🕒 Giờ trận xác định: {match.get('time')} {match.get('date')} | "
                f"{match.get('timing_state')}",
                flush=True,
            )
    except Exception as exc:
        match.setdefault("errors", []).append(f"metadata-only: {type(exc).__name__}: {exc}")
        print(
            f"   ⚠️ FLV đã xác minh nhưng chưa bổ sung được metadata: "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass


async def _http_fetch_text(
    context: BrowserContext,
    url: str,
    referer: str,
) -> tuple[int, dict[str, str], str, str]:
    try:
        response = await context.request.get(
            url,
            headers={
                "User-Agent": UA,
                "Referer": referer or TARGET_URL,
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
            },
            timeout=HTTP_DISCOVERY_TIMEOUT_SECONDS * 1000,
            fail_on_status_code=False,
        )
        body = await response.body()
        headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        content_type = headers.get("content-type", "")
        if len(body) > 3_000_000:
            body = body[:3_000_000]
        encoding = "utf-8"
        match = re.search(r"charset=([a-zA-Z0-9._-]+)", content_type)
        if match:
            encoding = match.group(1)
        return response.status, headers, body.decode(encoding, errors="ignore"), response.url
    except Exception as exc:
        return 0, {}, f"__HTTP_ERROR__:{type(exc).__name__}:{exc}", url


async def discover_http_candidates(
    context: BrowserContext,
    match: dict[str, Any],
    capture_callback: Any,
) -> int:
    if not HYBRID_HTTP_FIRST:
        return 0
    queue: list[tuple[str, str, int]] = [(match.get("url", ""), match.get("url", ""), 0)]
    visited: set[str] = set()
    discovered_before = 0
    followed = 0
    while queue and followed <= HTTP_DISCOVERY_MAX_FOLLOWS:
        url, referer, depth = queue.pop(0)
        if not url or url in visited:
            continue
        visited.add(url)
        followed += 1
        status, headers, text, final_url = await _http_fetch_text(context, url, referer)
        if status == 0:
            match.setdefault("errors", []).append(text.removeprefix("__HTTP_ERROR__:"))
            continue
        if depth == 0 and not match.get("blv"):
            blv_match = re.search(
                r"(?i)(?:\bBLV\b|bình\s*luận\s*viên)\s*[:\-]?\s*([A-Za-zÀ-ỹ0-9 _.-]{2,32})",
                clean_text(re.sub(r"<[^>]+>", " ", text)),
            )
            if blv_match:
                match["blv"] = normalize_blv_name(blv_match.group(1))
        base_url = final_url or url
        refs = extract_explicit_references(text, base_url)
        for ref in refs:
            raw = ref.get("url", "")
            kind = ref.get("kind", "reference")
            if not raw:
                continue
            capture_callback(
                raw,
                f"http/{kind}",
                headers={
                    "referer": base_url if depth else match.get("url", ""),
                    "user-agent": UA,
                    "origin": headers.get("origin", ""),
                },
                frame_url=base_url,
                status=None,
                content_type=headers.get("content-type", "") if kind == "stream" else "",
            )
            discovered_before += 1
            lower = raw.lower()
            if (
                depth < 1
                and kind in {"iframe", "reference"}
                and raw.startswith(("http://", "https://"))
                and any(marker in lower for marker in ("embed", "player", "live", "stream"))
                and ".m3u8" not in lower
                and ".flv" not in lower
            ):
                queue.append((raw, base_url, depth + 1))
    return discovered_before


async def fetch_stream(
    context: BrowserContext,
    match: dict[str, Any],
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    async with sem:
        match_name, time_str, blv_from_link = derive_match_info(
            match["url"], match.get("raw_title", ""), match.get("raw_time", "")
        )
        match["match_name"] = match_name
        match["time"] = clean_text(str(match.get("time", ""))) or time_str
        match["date"] = clean_text(str(match.get("date", ""))) or extract_date(match.get("raw_time", "")) or extract_date(match.get("raw_title", ""))
        match["time_source"] = match.get("time_source") or ("home-card" if match.get("time") else "")
        annotate_match_timing(match)
        match["blv"] = (
            normalize_blv_name(str(match.get("raw_blv", "")))
            or blv_from_link
            or normalize_blv_name(str(match.get("blv", "")))
        )
        match["streams"] = []
        match["stream_urls"] = []
        match["errors"] = []
        match["sport_group"] = classify_sport(
            match.get("sport_hint", ""),
            match.get("card_text", ""),
            match.get("raw_title", ""),
            match.get("url", ""),
            default=match.get("sport_group", "Bóng đá"),
        )

        scan_index = int(match.get("_scan_index", 0))
        scan_total = int(match.get("_scan_total", 0))
        prefix = f"[{scan_index}/{scan_total}] " if scan_index and scan_total else ""
        print(
            f"-> {prefix}Đang quét [{match['sport_group']}]: {match_name[:90]}",
            flush=True,
        )
        stream_map: dict[str, dict[str, Any]] = {}
        first_stream_at: float | None = None
        rate_limit_urls: set[str] = set()
        response_body_tasks: set[asyncio.Task[Any]] = set()

        def capture_url(
            raw_url: str,
            source: str,
            headers: dict[str, str] | None = None,
            frame_url: str = "",
            status: int | None = None,
            content_type: str = "",
            quality: str = "",
            parent_url: str = "",
        ) -> None:
            nonlocal first_stream_at
            normalized_headers = {
                str(k).lower(): str(v) for k, v in (headers or {}).items()
            }
            hint = stream_referer_hint(raw_url, frame_url)

            for stream_url in extract_stream_urls(raw_url, content_type):
                normalized = canonicalize_stream_url(stream_url)
                entry = stream_map.setdefault(
                    normalized,
                    {
                        "url": normalized,
                        "referer": "",
                        "origin": "",
                        "user_agent": "",
                        "status": None,
                        "statuses": [],
                        "content_type": "",
                        "sources": [],
                        "quality": "",
                        "parent_url": "",
                    },
                )

                referer = normalize_playback_referer(
                    normalized_headers.get("referer", "") or hint
                )
                # Chỉ lưu Origin khi request thật hoặc cơ chế dựng Gà Vàng cung cấp;
                # không tự suy đoán Origin cho các URL bắt được từ nguồn khác.
                origin = normalized_headers.get("origin", "")
                user_agent = normalized_headers.get("user-agent", "") or UA

                if referer:
                    entry["referer"] = referer
                if origin:
                    entry["origin"] = origin
                if user_agent:
                    entry["user_agent"] = user_agent
                if status is not None:
                    entry["status"] = status
                    if status not in entry["statuses"]:
                        entry["statuses"].append(status)
                if content_type:
                    entry["content_type"] = content_type
                normalized_quality = normalize_quality_hint(quality or raw_url)
                if normalized_quality:
                    entry["quality"] = normalized_quality
                if parent_url:
                    entry["parent_url"] = parent_url
                if source not in entry["sources"]:
                    entry["sources"].append(source)

                if first_stream_at is None:
                    first_stream_at = time.monotonic()
                if len(entry["sources"]) == 1:
                    print(f"   🎯 [{source}] {normalized}")

        derived_candidates = derived_gavang_stream_candidates(match.get("url", ""))
        for candidate in derived_candidates:
            capture_url(
                candidate["url"],
                candidate["source"],
                headers={
                    "referer": candidate["referer"],
                    "origin": candidate["origin"],
                    "user-agent": UA,
                },
                frame_url=match.get("url", ""),
                quality=candidate.get("quality", ""),
            )
        if derived_candidates:
            print(
                f"   ⚡ Dựng {len(derived_candidates)} FLV từ s8_live_stream_key; "
                "probe ngay trước khi tải trang/player.",
                flush=True,
            )
            derived_streams, derived_rejected = await finalize_stream_map(
                context, stream_map, match, log_prefix="FLV dựng: "
            )
            derived_verified = [
                entry for entry in derived_streams
                if entry.get("playability") == "verified"
            ]
            if derived_verified:
                match["scan_decision"] = "derived-flv-fast-path"
                match["rejected_streams"] = derived_rejected
                match["streams"] = derived_verified[:MAX_OUTPUT_STREAMS_PER_MATCH]
                match["stream_urls"] = [item["url"] for item in match["streams"]]
                print(
                    f"   🚀 FLV dựng đã xác minh={len(match['streams'])}; "
                    "chỉ mở trang nhẹ để lấy tên/giờ/BLV, không chạy lại scanner player.",
                    flush=True,
                )
                await enrich_verified_match_metadata(context, match)
                return match

            derived_pending = build_derived_pending_streams(
                match, derived_candidates, derived_rejected
            )
            if derived_pending:
                match["_derived_pending_streams"] = derived_pending
                print(
                    "   🟠 Giữ FLV dựng ở trạng thái pending | "
                    f"lý do={derived_pending[0].get('pending_reason')} | "
                    f"{derived_pending[0].get('url')}",
                    flush=True,
                )
                print(
                    "   🧾 Pending vẫn mở trang metadata nhẹ để lấy tên/giờ/BLV/logo; "
                    "không mở player.",
                    flush=True,
                )
                await enrich_verified_match_metadata(context, match)

            if match.get("derived_probe_only"):
                match["scan_decision"] = (
                    "derived-pending-only" if derived_pending else "derived-probe-only-miss"
                )
                match["rejected_streams"] = [
                    item for item in derived_rejected
                    if canonicalize_stream_url(str(item.get("url", "")))
                    not in {row.get("url") for row in derived_pending}
                ]
                match["streams"] = derived_pending
                match["stream_urls"] = [item["url"] for item in derived_pending]
                if derived_pending:
                    print(
                        "   ⏭️ URL thiếu giờ nhưng còn trên trang chủ; "
                        "ghi trạng thái pending và dừng probe nhanh, không mở player.",
                        flush=True,
                    )
                else:
                    print(
                        "   ⏭️ URL thiếu giờ/LIVE và FLV dựng chưa phát; "
                        "dừng ở probe nhanh, không mở trang/player.",
                        flush=True,
                    )
                return match

        http_reference_count = await discover_http_candidates(context, match, capture_url)
        if http_reference_count:
            print(
                f"   ⚡ HTTP-first phát hiện {len(stream_map)} URL media từ "
                f"{http_reference_count} tham chiếu player; chưa cần mở tab Chromium.",
                flush=True,
            )

        if stream_map:
            early_streams, early_rejected = await finalize_stream_map(
                context, stream_map, match, log_prefix="HTTP-first: "
            )
            verified_count = sum(
                1 for entry in early_streams if entry.get("playability") == "verified"
            )
            delta = match.get("minutes_to_kickoff")
            enough = verified_count >= 1
            far_with_result = isinstance(delta, int) and delta > DELTA_NEAR_MINUTES and bool(early_streams)
            if enough or far_with_result:
                if not verified_count and match.get("_derived_pending_streams"):
                    early_streams = list(match["_derived_pending_streams"])
                match["scan_decision"] = "http-first-complete"
                match["rejected_streams"] = early_rejected
                match["streams"] = early_streams
                match["stream_urls"] = [item["url"] for item in early_streams]
                print(
                    f"   🚀 Dừng sớm HTTP-first: verified={verified_count}, "
                    f"đầu ra={len(early_streams)}; không mở tab Chromium.",
                    flush=True,
                )
                return match

        delta = match.get("minutes_to_kickoff")
        if (
            isinstance(delta, int)
            and delta > DELTA_NEAR_MINUTES
            and match.get("_derived_pending_streams")
        ):
            match["scan_decision"] = "derived-pending-far-upcoming"
            match["streams"] = list(match["_derived_pending_streams"])
            match["stream_urls"] = [item["url"] for item in match["streams"]]
            print(
                f"   ⏭️ Trận còn {delta} phút; giữ FLV pending và bỏ qua Chromium, "
                "sẽ probe lại theo lịch delta.",
                flush=True,
            )
            return match
        if isinstance(delta, int) and delta > DELTA_NEAR_MINUTES and not stream_map:
            match["scan_decision"] = "http-only-far-upcoming"
            print(
                f"   ⏭️ Trận còn {delta} phút và HTTP chưa lộ stream; "
                "bỏ qua tab Chromium, sẽ kiểm tra lại theo lịch delta.",
                flush=True,
            )
            return match

        match["scan_decision"] = "browser-fallback"
        page = await context.new_page()
        await install_route_filter(page, homepage=False)

        async def inspect_response_body(response: Any) -> None:
            try:
                content_type = (response.headers.get("content-type", "") or "").lower()
                content_length = response.headers.get("content-length", "")
                if content_length and int(content_length) > 2_500_000:
                    return
                kind = stream_kind(response.url, content_type)
                textual = any(marker in content_type for marker in (
                    "json", "javascript", "text/", "mpegurl", "xml"
                )) or kind == "m3u8"
                if not textual:
                    return
                body = await response.body()
                if not body or len(body) > 2_500_000:
                    return
                text = body.decode("utf-8", errors="ignore")
                request = response.request
                try:
                    frame_url = request.frame.url if request.frame else ""
                except Exception:
                    frame_url = ""
                # Không quét URL bằng regex trong mọi JSON/JS/HTML nữa. Trang player
                # chứa danh sách cấu hình chung của nhiều BLV/kênh, khiến mỗi trận bị gán
                # hàng chục link không liên quan. Chỉ tách variant khi response chính là HLS.
                if kind == "m3u8" or "mpegurl" in content_type:
                    for variant in parse_hls_variants(text, response.url):
                        capture_url(
                            variant["url"], "hls/variant", headers=request.headers,
                            frame_url=frame_url, content_type="application/vnd.apple.mpegurl",
                            quality=variant.get("quality", ""),
                            parent_url=variant.get("parent_url", ""),
                        )
            except Exception:
                return

        def track_response_body(response: Any) -> None:
            task = asyncio.create_task(inspect_response_body(response))
            response_body_tasks.add(task)
            task.add_done_callback(response_body_tasks.discard)

        def handle_request(request: Any) -> None:
            try:
                frame_url = request.frame.url if request.frame else ""
            except Exception:
                frame_url = ""
            capture_url(
                request.url,
                f"request/{request.resource_type}",
                headers=request.headers,
                frame_url=frame_url,
            )

        def handle_response(response: Any) -> None:
            try:
                if response.status == 429 and response.url not in rate_limit_urls:
                    rate_limit_urls.add(response.url)
                    match["errors"].append(
                        f"HTTP 429 (tiếp tục quét, không restart): {response.url}"
                    )
                    print(f"   ⚠️ HTTP 429 nhưng vẫn tiếp tục quét full: {response.url}")

                req = response.request
                frame_url = req.frame.url if req.frame else ""
                content_type = response.headers.get("content-type", "")
                if stream_kind(response.url, content_type) == "m3u8" or "mpegurl" in content_type.lower():
                    track_response_body(response)
                capture_url(
                    response.url,
                    "response",
                    headers=req.headers,
                    frame_url=frame_url,
                    status=response.status,
                    content_type=content_type,
                )
            except Exception:
                capture_url(response.url, "response", status=response.status)

        def handle_page_error(error: Any) -> None:
            match["errors"].append(f"JS: {error}")

        def handle_console(message: Any) -> None:
            if message.type in {"error", "warning"}:
                text = str(message.text)
                if len(text) <= 500:
                    match["errors"].append(f"console/{message.type}: {text}")

        page.on("request", handle_request)
        page.on("response", handle_response)
        page.on("pageerror", handle_page_error)
        page.on("console", handle_console)

        try:
            await page.goto(match["url"], wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1200)

            blv_slug = (parse_qs(urlparse(match["url"]).query).get("blv") or [""])[0]
            metadata = await read_match_metadata(
                page, match["url"], match.get("match_name", ""), blv_slug
            )
            old_time = clean_text(str(match.get("time", "")))
            changes = apply_basic_match_metadata(match, metadata)
            if changes.get("time") and old_time:
                print(
                    f"   🕒 Sửa giờ trận từ {old_time} thành {changes['time']} "
                    f"theo nguồn {match.get('time_source', '')}",
                    flush=True,
                )

            if match.get("kickoff_iso"):
                delta = match.get("minutes_to_kickoff")
                state = match.get("timing_state")
                suffix = (
                    f"còn {delta} phút; cho phép giữ link pending"
                    if state == "upcoming-window"
                    else f"lệch {delta:+d} phút so với lúc quét"
                )
                print(
                    f"   🕒 Giờ trận xác định: {match.get('time')} {match.get('date')} | "
                    f"{state} | {suffix}",
                    flush=True,
                )
            else:
                print("   ⚠️ Chưa xác định đủ giờ trận; không giữ link chưa phát.", flush=True)

            match["sport_group"] = classify_sport(
                match.get("sport_hint", ""),
                metadata.get("sport_text", ""),
                match.get("card_text", ""),
                match.get("match_name", ""),
                match.get("url", ""),
                default=match.get("sport_group", "Bóng đá"),
            )

            merge_metadata_logos(match, metadata)

            for hinted_url in match.get("stream_hints") or []:
                capture_url(
                    str(hinted_url),
                    "home-card/stream-hint",
                    frame_url=match.get("url", ""),
                )

            for iframe_url in metadata.get("iframe_urls") or []:
                iframe_blv = extract_blv_from_url(iframe_url)
                if iframe_blv and not match.get("blv"):
                    match["blv"] = iframe_blv
                capture_url(iframe_url, "iframe/src", frame_url=iframe_url)

            for source_info in metadata.get("quality_sources") or []:
                if not isinstance(source_info, dict):
                    continue
                capture_url(
                    str(source_info.get("url") or ""),
                    "metadata/quality-source",
                    frame_url=match["url"],
                    quality=str(source_info.get("quality") or ""),
                )

            for previous in match.get("_previous_streams") or []:
                if not isinstance(previous, dict):
                    continue
                capture_url(
                    str(previous.get("url") or ""),
                    "previous-playlist",
                    headers={
                        "referer": str(previous.get("referer") or PLAYER_ORIGIN_FALLBACK + "/"),
                        "user-agent": str(previous.get("user_agent") or UA),
                    },
                    frame_url=match["url"],
                )

            match_wait_seconds = effective_stream_wait_seconds(match)
            allow_quality_scan = should_probe_quality_buttons(match, bool(stream_map))
            if match_wait_seconds < STREAM_WAIT_SECONDS:
                print(
                    f"   ⚡ Trận còn xa giờ đá; quét nhanh {match_wait_seconds}s "
                    f"thay vì {STREAM_WAIT_SECONDS}s",
                    flush=True,
                )

            activated_qualities: list[str] = []
            if allow_quality_scan:
                activated_qualities = await scan_quality_variants(page, capture_url)
                if activated_qualities:
                    print(
                        "   🎛️ Đã lần lượt thử các mức chất lượng: "
                        + ", ".join(activated_qualities),
                        flush=True,
                    )
                else:
                    quality_clicks = await stimulate_quality_variants(page)
                    if quality_clicks:
                        print(
                            f"   🎛️ Đã thử fallback {quality_clicks} nút/tuỳ chọn chất lượng",
                            flush=True,
                        )
            else:
                print(
                    "   ⏭️ Bỏ qua thao tác đổi FHD/HD vì trận còn xa và player chưa lộ stream",
                    flush=True,
                )

            deadline = time.monotonic() + match_wait_seconds
            quality_retry_done = False
            while time.monotonic() < deadline:
                await stimulate_player(page)
                for candidate in await collect_dom_stream_candidates(page):
                    capture_url(
                        candidate["url"],
                        f"dom/{candidate.get('source', 'candidate')}",
                        frame_url=candidate.get("frame_url", ""),
                        quality=candidate.get("quality", ""),
                    )

                if (
                    not FULL_SCAN
                    and first_stream_at is not None
                    and time.monotonic() - first_stream_at >= EXTRA_WAIT_AFTER_FIRST_STREAM
                ):
                    break
                elapsed = match_wait_seconds - max(0.0, deadline - time.monotonic())
                if allow_quality_scan and not quality_retry_done and elapsed >= max(5, match_wait_seconds * 0.55):
                    quality_retry_done = True
                    retry_qualities = await scan_quality_variants(page, capture_url)
                    if retry_qualities:
                        print(
                            "   🔁 Quét lại nguồn sau khi đổi chất lượng: "
                            + ", ".join(retry_qualities),
                            flush=True,
                        )
                await page.wait_for_timeout(1000)

        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"
            match["errors"].append(error_text)
            print(f"   ❌ {match_name[:70]} | {error_text}")
        finally:
            if response_body_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*list(response_body_tasks), return_exceptions=True),
                        timeout=6,
                    )
                except Exception:
                    pass
            try:
                for candidate in await collect_dom_stream_candidates(page):
                    capture_url(
                        candidate["url"],
                        f"dom/{candidate.get('source', 'final')}",
                        frame_url=candidate.get("frame_url", ""),
                        quality=candidate.get("quality", ""),
                    )
            except Exception:
                pass
            await page.close()

        if not match.get("scan_time_iso"):
            annotate_match_timing(match)
        match["streams"], match["rejected_streams"] = await finalize_stream_map(
            context, stream_map, match
        )
        verified_after_browser = any(
            item.get("playability") == "verified" for item in match["streams"]
        )
        if not verified_after_browser and match.get("_derived_pending_streams"):
            pending_urls = {item.get("url") for item in match["_derived_pending_streams"]}
            match["streams"] = [
                item for item in match["streams"] if item.get("url") not in pending_urls
            ] + list(match["_derived_pending_streams"])
            match["streams"], pending_quality_rejected = select_best_quality_streams(
                match["streams"], MAX_OUTPUT_STREAMS_PER_MATCH
            )
            match["rejected_streams"].extend(pending_quality_rejected)
            retained = {item.get("url") for item in match["streams"]}
            match["rejected_streams"] = [
                item for item in match["rejected_streams"]
                if not (
                    item.get("url") in retained
                    and item.get("playability") == "rejected"
                )
            ]
        match["stream_urls"] = [item["url"] for item in match["streams"]]

        if match["streams"]:
            verified_count = sum(
                1 for entry in match["streams"]
                if entry.get("playability") == "verified"
            )
            fallback_count = len(match["streams"]) - verified_count
            print(
                f"   📌 Kết quả cuối: verified={verified_count} | "
                f"fallback={fallback_count} | rejected={len(match['rejected_streams'])}",
                flush=True,
            )
            for entry in match["streams"]:
                probe = entry.get("probe") or {}
                print(
                    f"   ✅ Stream {entry.get('playability', 'unknown')} | "
                    f"HTTP={probe.get('status') or entry.get('status') or 'N/A'} | "
                    f"referer={entry.get('referer', '')} | "
                    f"logo={'có' if match.get('logo') else 'không'} | "
                    f"BLV={match.get('blv') or 'không rõ'} | "
                    f"chất lượng={entry.get('quality') or 'không rõ'} | "
                    f"giờ={match.get('time') or 'không rõ'} {match.get('date') or ''} | "
                    f"timing={match.get('timing_state') or 'unknown'}"
                )
        else:
            print(f"   ⚠️ Không có stream đủ tin cậy: {match_name[:85]}")

        return match


async def collect_home_links(context: BrowserContext, home_url: str = TARGET_URL) -> list[dict[str, Any]]:
    page = await context.new_page()
    await install_route_filter(page, homepage=True)
    print(f"👉 Đang mở trang chủ Gà Vàng: {home_url}")

    try:
        await page.goto(home_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(HOME_WAIT_MS)

        for _ in range(5):
            await page.evaluate("window.scrollBy(0, Math.max(700, window.innerHeight));")
            await page.wait_for_timeout(700)

        result = await page.evaluate(
            r"""() => {
                const items = [];
                const seen = new Set();
                const clean = (v) => String(v || "").replace(/\s+/g, " ").trim();

                function normalizeHref(value) {
                    try { return new URL(value, location.href).href; }
                    catch (_) { return ""; }
                }

                function isMatchHref(value) {
                    const href = normalizeHref(value);
                    if (!href) return false;
                    try {
                        const path = new URL(href).pathname;
                        return /^\/s8-live\/\d+(?:\/|$)/i.test(path);
                    } catch (_) { return false; }
                }

                function imageCandidates(scope, matchText = "") {
                    const scored = [];
                    if (!scope) return [];
                    const teamParts = clean(matchText).split(/\s+vs\s+/i);
                    const homeTokens = clean(teamParts[0] || "").toLowerCase().split(/[^a-z0-9À-ỹ]+/i).filter((v) => v.length >= 4);
                    const awayTokens = clean((teamParts[1] || "").split(" - ")[0]).toLowerCase().split(/[^a-z0-9À-ỹ]+/i).filter((v) => v.length >= 4);
                    scope.querySelectorAll("img").forEach((img, index) => {
                        const context = clean([
                            img.alt, img.title, img.className, img.parentElement?.innerText,
                            img.parentElement?.className, img.parentElement?.parentElement?.innerText,
                            img.parentElement?.parentElement?.className
                        ].join(" ")).slice(0, 500);
                        const lower = context.toLowerCase();
                        let score = 0;
                        if (homeTokens.some((token) => lower.includes(token))) score += 60;
                        else if (awayTokens.some((token) => lower.includes(token))) score += 35;
                        if (/team|club|home|away|doi|đội/.test(lower)) score += 12;
                        if (/logo/.test(lower)) score += 4;
                        if (/avatar|blv|comment|banner|advert|ads|flag|league/.test(lower)) score -= 35;
                        const width = img.naturalWidth || img.width || 0;
                        const height = img.naturalHeight || img.height || 0;
                        if (width && height && Math.abs(width - height) <= Math.max(width, height) * 0.35) score += 5;
                        score -= index * 0.01;

                        const values = [img.currentSrc, img.src, img.getAttribute("src"),
                            img.getAttribute("data-src"), img.getAttribute("data-original"),
                            img.getAttribute("data-lazy-src")];
                        [img.getAttribute("srcset"), img.getAttribute("data-srcset")].filter(Boolean)
                            .forEach((set) => set.split(",").forEach((part) => values.push(part.trim().split(/\s+/)[0])));
                        values.filter((value) => typeof value === "string" && value.trim() && !/\[object\s+object\]/i.test(value)).forEach((value) => {
                            try { value = new URL(value, location.href).href; } catch (_) {}
                            if (/^https?:\/\//i.test(value)) {
                                scored.push({url: value, score, context, source: "home-card"});
                            }
                        });
                    });
                    const out = [];
                    const unique = new Set();
                    scored.sort((a, b) => b.score - a.score).forEach((item) => {
                        if (!unique.has(item.url)) { unique.add(item.url); out.push(item); }
                    });
                    return out.slice(0, 20);
                }

                function sourceLogoCandidates() {
                    const scored = [];
                    const seenSource = new Set();
                    const add = (value, score = 0, context = "") => {
                        if (typeof value !== "string") return;
                        let url = value.trim();
                        if (!url || /\[object\s+object\]/i.test(url) || url.startsWith("data:") || url.startsWith("blob:")) return;
                        try { url = new URL(url, location.href).href; } catch (_) { return; }
                        if (!/^https?:\/\//i.test(url) || seenSource.has(url)) return;
                        seenSource.add(url);
                        const lower = clean(`${context} ${url}`).toLowerCase();
                        if (/gavang|gà vàng|site-logo|header|footer/.test(lower)) score += 80;
                        if (/logo/.test(lower)) score += 20;
                        if (/favicon/.test(lower)) score += 5;
                        if (/advert|banner|avatar|blv|comment/.test(lower)) score -= 80;
                        scored.push({url, score, context: clean(context), source: "source-logo"});
                    };
                    document.querySelectorAll(
                        "header img, footer img, img[alt*='gavang' i], img[title*='gavang' i], " +
                        "[class*='site-logo'] img, [class*='logo'] img"
                    ).forEach((img) => {
                        const context = clean([img.alt, img.title, img.className, img.parentElement?.className, img.parentElement?.innerText].filter(Boolean).join(" "));
                        [img.currentSrc, img.src, img.getAttribute("src"), img.getAttribute("data-src"), img.getAttribute("data-lazy-src")].forEach((value) => add(value, 10, context));
                    });
                    document.querySelectorAll("link[rel~='icon'], link[rel='apple-touch-icon'], link[rel='shortcut icon']")
                        .forEach((node) => add(node.href || node.getAttribute("href"), 5, `favicon ${node.rel || ""}`));
                    scored.sort((a, b) => b.score - a.score);
                    return scored.slice(0, 12);
                }

                function streamHints(scope) {
                    if (!scope) return [];
                    const out = new Set();
                    const add = (value) => {
                        const text = clean(value);
                        if (!text) return;
                        (text.replace(/\\\//g, "/").match(/https?:\/\/[^"' <>\n\r]+?(?:\.m3u8|\.flv)(?:\?[^"' <>\n\r]*)?/gi) || [])
                            .forEach((url) => out.add(url));
                    };
                    scope.querySelectorAll("iframe,video,source,[data-stream],[data-url],[data-src]").forEach((el) => {
                        [el.src, el.currentSrc, el.getAttribute("src"), el.getAttribute("data-stream"),
                         el.getAttribute("data-url"), el.getAttribute("data-src")].forEach(add);
                    });
                    add(scope.innerHTML || "");
                    return Array.from(out).slice(0, 12);
                }

                function findContainer(a) {
                    const target = normalizeHref(a.href || a.getAttribute("href") || "");
                    let node = a;
                    for (let depth = 0; node && depth < 9; depth += 1, node = node.parentElement) {
                        const links = Array.from(node.querySelectorAll?.("a[href]") || [])
                            .map((el) => normalizeHref(el.href || el.getAttribute("href") || ""))
                            .filter(isMatchHref);
                        const uniqueLinks = Array.from(new Set(links));
                        const text = clean(node.innerText || node.textContent || "");
                        if (uniqueLinks.length === 1 && uniqueLinks[0] === target && /\bvs\b/i.test(text)) {
                            return node;
                        }
                    }
                    return a.closest(
                        "[data-match-id], [data-event-id], [class*='match-card'], " +
                        "[class*='match-item'], [class*='game-card'], [class*='fixture'], article, li"
                    ) || a.parentElement || a;
                }

                function sportContext(a, container) {
                    const parts = [];
                    const add = (value) => {
                        const fixed = clean(value);
                        if (fixed && fixed.length <= 300 && !parts.includes(fixed)) parts.push(fixed);
                    };
                    const inspect = (node) => {
                        if (!node) return;
                        [
                            node.getAttribute?.("data-sport"),
                            node.getAttribute?.("data-category"),
                            node.getAttribute?.("data-type"),
                            node.getAttribute?.("aria-label"),
                            node.id,
                            node.className
                        ].forEach(add);
                        const heading = node.querySelector?.(
                            ":scope > h1, :scope > h2, :scope > h3, :scope > h4, " +
                            ":scope > [class*='sport-title'], :scope > [class*='category-title']"
                        );
                        add(heading?.innerText || heading?.textContent);
                    };

                    inspect(a);
                    inspect(container);
                    let node = container;
                    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
                        inspect(node);
                        let previous = node.previousElementSibling;
                        for (let step = 0; previous && step < 3; step += 1, previous = previous.previousElementSibling) {
                            if (/^H[1-6]$/.test(previous.tagName || "") ||
                                /sport|category|tab|section/i.test(String(previous.className || ""))) {
                                add(previous.innerText || previous.textContent);
                            }
                        }
                    }
                    return parts.join(" | ");
                }

                function cleanTeamName(value) {
                    let text = clean(value);
                    if (!text || text.length > 100) return "";
                    text = text.replace(/^(?:home|away|đội nhà|đội khách)\s*[:\-]?\s*/i, "");
                    if (!text || /(?:đang diễn ra|sắp diễn ra|\blive\b|trực tiếp|bình luận|tỷ số|kèo|\d{1,2}:\d{2})/i.test(text)) return "";
                    if (/^[0-9\-: ]+$/.test(text)) return "";
                    return text;
                }

                function inferMatchTitle(container, explicitTitle = "") {
                    const direct = clean(explicitTitle);
                    if (/\bvs\b/i.test(direct)) return direct;

                    // Nhiều card smorf.io đặt hai đội ở hai node riêng, nhưng text của
                    // card vẫn có một dòng "Đội A VS Đội B". Lấy dòng đó trước khi
                    // phải fallback sang slug rút gọn như queensland-perth-ausffa.
                    const cardLines = String(container?.innerText || container?.textContent || "")
                        .split(/[\n|•]+/).map(clean).filter(Boolean);
                    const vsLine = cardLines.find((line) => /\bvs\b/i.test(line) && line.length <= 220);
                    if (vsLine) return vsLine;

                    const homeSelectors = [
                        "[data-home-team]", "[data-home-name]", "[data-home]",
                        "[class*='home-team'] [class*='name']", "[class*='home'] [class*='team-name']",
                        "[class*='team-home'] [class*='name']"
                    ];
                    const awaySelectors = [
                        "[data-away-team]", "[data-away-name]", "[data-away]",
                        "[class*='away-team'] [class*='name']", "[class*='away'] [class*='team-name']",
                        "[class*='team-away'] [class*='name']"
                    ];
                    const attrText = (el, names) => {
                        if (!el) return "";
                        for (const name of names) {
                            const value = cleanTeamName(el.getAttribute?.(name));
                            if (value) return value;
                        }
                        return cleanTeamName(el.innerText || el.textContent);
                    };
                    const firstFrom = (selectors, attrs) => {
                        for (const selector of selectors) {
                            const el = container?.querySelector?.(selector);
                            const value = attrText(el, attrs);
                            if (value) return value;
                        }
                        return "";
                    };
                    const home = firstFrom(homeSelectors, ["data-home-team", "data-home-name", "data-home", "data-name", "data-team-name"]);
                    const away = firstFrom(awaySelectors, ["data-away-team", "data-away-name", "data-away", "data-name", "data-team-name"]);
                    if (home && away && home.toLowerCase() !== away.toLowerCase()) return `${home} vs ${away}`;

                    const names = [];
                    const seenNames = new Set();
                    container?.querySelectorAll?.(
                        "[data-team-name], [class*='team-name'], [class*='club-name'], " +
                        "[class*='team'] [class*='name'], [class*='club'] [class*='name'], " +
                        "[class*='team'], [class*='club'], img[alt], img[title]"
                    ).forEach((el) => {
                        const value = cleanTeamName(
                            el.getAttribute?.("data-team-name") || el.getAttribute?.("data-name") ||
                            el.getAttribute?.("alt") || el.getAttribute?.("title") ||
                            el.innerText || el.textContent
                        );
                        const key = value.toLowerCase();
                        if (value && !seenNames.has(key)) { seenNames.add(key); names.push(value); }
                    });
                    if (names.length >= 2) return `${names[0]} vs ${names[1]}`;
                    return direct;
                }

                function extractCardTime(container, anchor) {
                    const values = [];
                    const add = (value) => { const fixed = clean(value); if (fixed) values.push(fixed); };
                    [anchor, container].filter(Boolean).forEach((node) => {
                        ["datetime", "data-time", "data-start", "data-date", "data-kickoff",
                         "data-start-time", "data-match-time", "data-event-time"].forEach((name) => add(node.getAttribute?.(name)));
                    });
                    container?.querySelectorAll?.(
                        "time, [datetime], [data-time], [data-start], [data-date], [data-kickoff], " +
                        "[data-start-time], [data-match-time], [data-event-time], " +
                        "[class*='kickoff'], [class*='match-time'], [class*='event-time'], " +
                        "[class*='start-time'], [class*='match-date'], [class*='event-date']"
                    ).forEach((el) => {
                        ["datetime", "data-time", "data-start", "data-date", "data-kickoff",
                         "data-start-time", "data-match-time", "data-event-time"].forEach((name) => add(el.getAttribute?.(name)));
                        add(el.innerText || el.textContent);
                    });
                    add(container?.innerText || container?.textContent);
                    return values.find((value) => /(?:^|\D)(?:[01]?\d|2[0-3])[:h.]?[0-5]\d(?:\D|$)/i.test(value) || /\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value)) || "";
                }

                function extractCardBlv(container) {
                    const selectors = [
                        "[data-blv].active", "[data-commentator].active", "[data-blv]", "[data-commentator]",
                        "[class*='blv-name']", "[class*='commentator-name']", "[class*='blv']", "[class*='commentator']"
                    ];
                    for (const selector of selectors) {
                        const el = container?.querySelector?.(selector);
                        const value = clean(el?.getAttribute?.("data-name") || el?.getAttribute?.("data-blv") ||
                            el?.getAttribute?.("data-commentator") || el?.innerText || el?.textContent);
                        if (value && value.length <= 160) return value;
                    }
                    const text = clean(container?.innerText || container?.textContent || "");
                    const match = text.match(/(?:BLV|Bình luận viên)\s*[:\-–—]?\s*([^|•\n]{2,80})/i);
                    return match ? clean(match[1]) : "";
                }

                function addItem(
                    hrefValue,
                    titleValue = "",
                    cardText = "",
                    timeValue = "",
                    logos = [],
                    sportHint = "",
                    mediaHints = [],
                    rawBlv = ""
                ) {
                    const href = normalizeHref(hrefValue);
                    if (!isMatchHref(href) || seen.has(href)) return;
                    seen.add(href);
                    const parts = new URL(href).pathname.split("/").filter(Boolean);
                    const fallback = decodeURIComponent(parts[parts.length - 1] || href)
                        .replace(/-vs-/gi, " vs ").replace(/-/g, " ");
                    items.push({
                        url: href,
                        raw_title: clean(titleValue || cardText || fallback),
                        card_text: clean(cardText),
                        raw_time: clean(timeValue),
                        logo: logos[0]?.url || "",
                        team_logos: logos.slice(0, 12).map((item) => item.url),
                        logo_candidates: logos.slice(0, 20),
                        sport_hint: clean(sportHint),
                        raw_blv: clean(rawBlv),
                        stream_hints: Array.from(new Set(mediaHints || [])).slice(0, 12),
                    });
                }

                document.querySelectorAll("a[href]").forEach((a) => {
                    const href = a.href || a.getAttribute("href") || "";
                    if (!isMatchHref(href)) return;
                    const container = findContainer(a);
                    const cardText = clean(container?.innerText || a.innerText || "");
                    const sportHint = sportContext(a, container);

                    const explicitTitle = clean(
                        Array.from(container?.querySelectorAll(
                            "h1, h2, h3, [class*='match-title'], [class*='match-name'], [class*='event-title']"
                        ) || []).find((el) => /\bvs\b/i.test(clean(el.innerText || el.textContent)))?.innerText ||
                        a.innerText || a.title || a.getAttribute("aria-label")
                    );
                    const inferredTitle = inferMatchTitle(container, explicitTitle);
                    const timeValue = extractCardTime(container, a);
                    const rawBlv = extractCardBlv(container);

                    addItem(
                        href,
                        inferredTitle,
                        cardText,
                        timeValue,
                        imageCandidates(container, inferredTitle || cardText),
                        sportHint,
                        streamHints(container),
                        rawBlv
                    );
                });

                const htmlText = document.documentElement?.innerHTML || "";
                const normalizedHtml = htmlText.replace(/\\\//g, "/")
                    .replace(/&amp;/g, "&").replace(/\\u002F/gi, "/");
                const patterns = [
                    /https?:\/\/[^"' <>\n\r]+\/s8-live\/\d+\/[^"' <>\n\r]+/gi,
                    /\/s8-live\/\d+\/[a-z0-9][a-z0-9._~!$&'()*+,;=:@%\/-]*/gi,
                ];
                for (const pattern of patterns) {
                    (normalizedHtml.match(pattern) || []).forEach((href) => addItem(href));
                }

                const anchors = Array.from(document.querySelectorAll("a[href]"));
                return {
                    items,
                    source_logo_candidates: sourceLogoCandidates(),
                    diagnostics: {
                        final_url: location.href,
                        title: document.title || "",
                        anchor_count: anchors.length,
                        html_length: normalizedHtml.length,
                        sample_hrefs: anchors.slice(0, 20).map((a) => a.href || "")
                    }
                };
            }"""
        )

        raw_links = list(result.get("items") or [])
        exact_home_map = await read_exact_fixture_script_metadata(page, raw_links)
        exact_home_count = 0
        for item in raw_links:
            exact = exact_home_map.get(gavang_match_identity(str(item.get("url", ""))))
            if not isinstance(exact, dict):
                continue
            exact_title = clean_text(str(exact.get("title", "")))
            if exact_title and not title_stream_key_confidence(exact_title, str(item.get("url", "")))["contradictory"]:
                if _match_title_score(exact_title) > _match_title_score(str(item.get("raw_title", ""))):
                    item["raw_title"] = exact_title
            exact_times = list(exact.get("time_candidates") or [])
            if exact_times:
                best_exact_time = max(exact_times, key=lambda row: int(row.get("score") or 0))
                if extract_time(str(best_exact_time.get("value", ""))):
                    item["raw_time"] = str(best_exact_time.get("value", ""))
                    item["time_source"] = str(best_exact_time.get("source", "exact-fixture-script"))
            exact_blv = normalize_blv_name(clean_text(str(exact.get("blv", ""))))
            if exact_blv:
                item["raw_blv"] = exact_blv
            exact_logos = [normalize_logo_url(value, str(item.get("url", ""))) for value in exact.get("logos", []) or []]
            exact_logos = [value for value in exact_logos if value]
            if exact_logos:
                item.setdefault("team_logos", [])[:0] = exact_logos
                item.setdefault("logo_candidates", [])[:0] = [
                    {"url": value, "score": 140, "context": "exact fixture script", "source": "exact-fixture-script"}
                    for value in exact_logos
                ]
                item["logo"] = exact_logos[0]
            if exact_title or exact_times or exact_blv or exact_logos:
                item["exact_fixture_script_found"] = True
                exact_home_count += 1
        if exact_home_count:
            print(f"🧩 Exact-fixture script bổ sung metadata cho {exact_home_count}/{len(raw_links)} card Gà Vàng.", flush=True)
        links, duplicate_count = dedupe_home_links(raw_links)
        source_logo_candidates = list(result.get("source_logo_candidates") or [])
        source_logo = choose_source_logo(source_logo_candidates, home_url)
        for item in links:
            item["source_logo"] = source_logo
            item["source_logo_candidates"] = source_logo_candidates
        if duplicate_count:
            print(
                f"🧹 Gộp {duplicate_count} URL/card trùng fixture Gà Vàng: "
                f"{len(raw_links)} -> {len(links)} trận duy nhất.",
                flush=True,
            )
        for item in links:
            item["source_home_url"] = home_url
        initial_logo_usage = Counter(
            item.get("logo", "") for item in links if item.get("logo")
        )
        for item in links:
            if item.get("logo") and initial_logo_usage[item["logo"]] > 1:
                item["logo"] = ""
                # Giữ candidate để trang chi tiết chấm lại, nhưng không ưu tiên logo lặp từ card cha.
                for candidate in item.get("logo_candidates", []) or []:
                    if isinstance(candidate, dict) and initial_logo_usage[candidate.get("url", "")] > 1:
                        candidate["score"] = float(candidate.get("score") or 0) - 80
        for item in links:
            item["sport_group"] = classify_sport(
                item.get("sport_hint", ""),
                item.get("card_text", ""),
                item.get("raw_title", ""),
                item.get("url", ""),
            )
        diagnostics = result.get("diagnostics") or {}
        print(
            "ℹ️ Trang chủ: "
            f"nguồn={home_url} | final={diagnostics.get('final_url', '')} | "
            f"title={diagnostics.get('title', '')!r} | "
            f"anchors={diagnostics.get('anchor_count', 0)} | "
            f"html={diagnostics.get('html_length', 0)} ký tự | "
            f"match_links={len(links)} | "
            f"source_logo={'có' if source_logo else 'không'}"
        )

        if links:
            counts = Counter(item.get("sport_group", "Khác") for item in links)
            summary = " | ".join(
                f"{group}={counts[group]}" for group in SPORT_GROUP_ORDER if counts[group]
            )
            print(f"📂 Phân loại link trang chủ: {summary}", flush=True)

        if not links:
            try:
                Path(OUTPUT_HOME_DEBUG_HTML).write_text(await page.content(), encoding="utf-8")
                await page.screenshot(path=OUTPUT_HOME_DEBUG_PNG, full_page=True)
                print(f"⚠️ Đã lưu trang debug: {OUTPUT_HOME_DEBUG_HTML}, {OUTPUT_HOME_DEBUG_PNG}")
            except Exception as debug_exc:
                print(f"⚠️ Không lưu được trang debug: {debug_exc}")
        return links

    except Exception as exc:
        print(f"❌ Không lấy được danh sách trang chủ: {type(exc).__name__}: {exc}")
        return []
    finally:
        await page.close()


async def collect_home_links_with_failover(context: BrowserContext) -> list[dict[str, Any]]:
    """Thử lần lượt các domain Gà Vàng đã cấu hình; chọn domain có card trận."""
    attempts: list[tuple[str, int]] = []
    for home_url in HOME_URLS:
        links = await collect_home_links(context, home_url)
        attempts.append((home_url, len(links)))
        if links:
            print(f"✅ Chọn miền Gà Vàng: {home_url} | trận={len(links)}", flush=True)
            return links
        print(f"⚠️ Miền Gà Vàng không có card trận, thử miền tiếp theo: {home_url}", flush=True)
    print("❌ Không miền Gà Vàng nào trả được card trận: " + ", ".join(f"{url}={count}" for url, count in attempts), flush=True)
    return []


def remove_cross_match_shared_streams(results: list[dict[str, Any]]) -> int:
    """Loại URL player chung bị gán cho nhiều fixture Gà Vàng khác nhau.

    Log thực tế cho thấy một HLS S3 placeholder giống hệt bị bắt ở nhiều trang trận.
    Một URL phát không thể đại diện đồng thời cho nhiều fixture khác nhau; giữ nó sẽ
    làm all_live.m3u có một trận ngẫu nhiên nhưng phát sai nội dung.
    """
    usage: dict[str, set[str]] = {}
    for row in results:
        fixture = match_id_from_url(str(row.get("url", ""))) or normalize_search_text(
            str(row.get("match_name") or row.get("raw_title") or "")
        )
        for stream in row.get("streams") or []:
            url = canonicalize_stream_url(str(stream.get("url", "")))
            if url and fixture:
                usage.setdefault(url, set()).add(fixture)

    shared = {url for url, fixtures in usage.items() if len(fixtures) > 1}
    if not shared:
        return 0

    removed = 0
    for row in results:
        kept = []
        for stream in row.get("streams") or []:
            url = canonicalize_stream_url(str(stream.get("url", "")))
            if url not in shared:
                kept.append(stream)
                continue
            removed += 1
            rejected = dict(stream)
            rejected["playability"] = "rejected"
            rejected["reject_reason"] = "URL player chung xuất hiện ở nhiều fixture Gà Vàng"
            row.setdefault("rejected_streams", []).append(rejected)
        row["streams"] = kept
        row["stream_urls"] = [item.get("url", "") for item in kept if item.get("url")]

    print(
        f"⚠️ Loại {removed} lượt gán stream dùng chung giữa nhiều fixture "
        f"({len(shared)} URL); tránh ghi nhầm player placeholder vào M3U.",
        flush=True,
    )
    return removed


def escape_m3u_text(value: str) -> str:
    return re.sub(r"[\r\n]+", " ", value or "").replace('"', "'").strip()


def header_json(user_agent: str, referer: str, origin: str = "") -> str:
    values = {"User-Agent": user_agent}
    if referer:
        values["Referer"] = referer
    if origin:
        values["Origin"] = origin
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

def escape_pipe_header(value: str) -> str:
    """Mã hóa giá trị protocol-option để URL không vỡ bởi khoảng trắng, &, | hoặc %."""
    return quote(clean_text(value), safe=":/().,;=-_")


def android_stream_url(
    stream_url: str,
    user_agent: str,
    referer: str,
    origin: str = "",
) -> str:
    """Kodi/Android pipe syntax, có đủ header được request thật của Gà Vàng dùng."""
    headers = [f"User-Agent={escape_pipe_header(user_agent)}"]
    if referer:
        headers.append(f"Referer={escape_pipe_header(referer)}")
    if origin:
        headers.append(f"Origin={escape_pipe_header(origin)}")
    return stream_url + "|" + "&".join(headers)

def ensure_output_logos(results: list[dict[str, Any]]) -> dict[str, int]:
    """Bảo đảm mọi stream Gà Vàng có tvg-logo hợp lệ, không bao giờ ghi [object Object]."""
    stats = {"team": 0, "source_fallback": 0, "invalid_removed": 0}
    for result in results:
        base = str(result.get("url") or TARGET_URL)
        current = normalize_logo_url(result.get("logo"), base)
        if current and is_good_logo_url(current):
            result["logo"] = current
            result["logo_is_fallback"] = False
            result["logo_source"] = result.get("logo_source") or "team"
            stats["team"] += 1
            continue
        if result.get("logo"):
            stats["invalid_removed"] += 1
        candidates = list(result.get("source_logo_candidates") or [])
        candidates.append(result.get("source_logo"))
        fallback = choose_source_logo(candidates, base)
        result["logo"] = fallback
        result["logo_is_fallback"] = True
        result["logo_source"] = "gavang-source-fallback"
        stats["source_fallback"] += 1
    return stats


def write_outputs(results: list[dict[str, Any]]) -> tuple[int, int]:
    """
    Tạo 3 playlist:
      - gavang_live.m3u: playlist phổ thông, URL nguyên bản + EXTHTTP/EXTVLCOPT.
      - gavang_live_pipe.m3u: biến thể Kodi-style URL|Header=Value.
      - gavang_live_vlc.m3u: URL nguyên bản + EXTVLCOPT dành riêng VLC.

    Không gắn pipe headers vào playlist mặc định vì nhiều IPTV player Android
    coi phần sau dấu | là một phần URL và báo lỗi phát kênh.
    """
    for output_path in (OUTPUT_M3U, OUTPUT_PIPE_M3U, OUTPUT_VLC_M3U):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    resolve_duplicate_logos(results)
    logo_stats = ensure_output_logos(results)
    if results:
        print(
            "🖼️ Logo Gà Vàng: "
            f"logo đội={logo_stats['team']} | fallback nguồn={logo_stats['source_fallback']} | "
            f"đã loại URL lỗi={logo_stats['invalid_removed']}",
            flush=True,
        )

    universal_lines = ["#EXTM3U"]
    pipe_lines = ["#EXTM3U"]
    vlc_lines = ["#EXTM3U"]

    written_streams: set[str] = set()
    match_keys_with_streams: set[str] = set()
    count_links = 0
    playability_counts: Counter[str] = Counter()

    sorted_results = sorted(
        results,
        key=lambda item: (
            SPORT_GROUP_RANK.get(item.get("sport_group", "Khác"), 999),
            item.get("time") or "99:99",
            clean_text(item.get("match_name") or item.get("raw_title") or "").lower(),
        ),
    )

    group_stream_counts: Counter[str] = Counter()

    for result in sorted_results:
        streams = result.get("streams") or [
            {"url": value} for value in (result.get("stream_urls") or [])
        ]
        if not streams:
            continue

        match_name = result.get("match_name") or result.get("raw_title") or "Gà Vàng TV"
        time_str = result.get("time") or ""
        date_str = result.get("date") or ""
        blv = result.get("blv") or ""
        sport_group = result.get("sport_group") or classify_sport(
            result.get("sport_hint", ""),
            result.get("card_text", ""),
            match_name,
            result.get("url", ""),
        )
        if sport_group not in SPORT_GROUP_RANK:
            sport_group = "Khác"
        # resolve_duplicate_logos() đã chọn logo cuối cùng và loại logo dùng nhầm
        # cho nhiều trận. Không chấm lại ở đây vì có thể vô tình chọn lại logo lỗi.
        logo = result.get("logo", "")

        # Lịch sẽ được dựng riêng theo trạng thái từng stream ở dưới. Không để
        # ngày đơn lẻ trông như một lịch đã đầy đủ.
        display_name_core = match_name
        if blv and blv.lower() not in display_name_core.lower():
            display_name_core += f" [BLV {blv}]"
        logo = escape_m3u_text(logo)

        unique_streams = [item for item in streams if item.get("url") not in written_streams]
        if not unique_streams:
            continue

        match_keys_with_streams.add(f"{match_name}|{blv}|{time_str}|{date_str}")
        for index, stream_info in enumerate(unique_streams, start=1):
            stream_url = decode_url_repeatedly(stream_info.get("url", ""))
            if not stream_url:
                continue
            written_streams.add(stream_url)
            playability_counts[clean_text(stream_info.get("playability") or "unknown")] += 1

            is_pending = stream_info.get("playability") == "upcoming-pending"
            if time_str and date_str:
                schedule_label = f"{time_str} {date_str}"
            elif date_str:
                schedule_label = f"CHƯA CÓ GIỜ {date_str}"
            elif time_str:
                schedule_label = f"{time_str} CHƯA RÕ NGÀY"
            elif is_pending:
                schedule_label = "CHƯA CÓ LỊCH"
            else:
                schedule_label = ""
            raw_display_base = f"[{schedule_label}] {display_name_core}" if schedule_label else display_name_core
            stream_display_base = escape_m3u_text(raw_display_base)
            display_name = stream_display_base
            quality = normalize_quality_hint(stream_info.get("quality", ""))
            if len(unique_streams) > 1 and not quality:
                display_name += f" (Luồng {index})"

            referer = normalize_playback_referer(
                stream_info.get("referer") or PLAYER_ORIGIN_FALLBACK + "/"
            )
            user_agent = clean_text(stream_info.get("user_agent") or UA)
            origin = clean_text(stream_info.get("origin") or origin_from_url(result.get("url", "")))
            kind = stream_kind(stream_url, stream_info.get("content_type", ""))
            if kind:
                suffix = f"{quality} {kind.upper()}" if quality else kind.upper()
                display_name += f" [{suffix}]"

            channel_id = channel_id_for(result, stream_url, index)
            attributes = (
                f'tvg-id="{escape_m3u_text(channel_id)}" '
                f'tvg-name="{escape_m3u_text(stream_display_base)}" '
                f'group-title="{escape_m3u_text(sport_group)}"'
            )
            if logo:
                attributes += f' tvg-logo="{logo}"'
            extinf = f"#EXTINF:-1 {attributes},{display_name}"

            universal_lines.extend([
                extinf,
                f"#EXTVLCOPT:http-referrer={referer}",
                f"#EXTVLCOPT:http-user-agent={user_agent}",
                "#EXTVLCOPT:http-reconnect=true",
                f"#EXTHTTP:{header_json(user_agent, referer, origin)}",
                stream_url,
            ])

            pipe_lines.extend([
                extinf,
                f"#EXTVLCOPT:http-referrer={referer}",
                f"#EXTVLCOPT:http-user-agent={user_agent}",
                "#EXTVLCOPT:http-reconnect=true",
                f"#EXTHTTP:{header_json(user_agent, referer, origin)}",
                android_stream_url(stream_url, user_agent, referer, origin),
            ])

            vlc_lines.extend([
                extinf,
                f"#EXTVLCOPT:http-referrer={referer}",
                f"#EXTVLCOPT:http-user-agent={user_agent}",
                "#EXTVLCOPT:http-reconnect=true",
                stream_url,
            ])

            group_stream_counts[sport_group] += 1
            count_links += 1

    Path(OUTPUT_DEBUG).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    output_sets = (
        (Path(OUTPUT_M3U), universal_lines, "phổ thông"),
        (Path(OUTPUT_PIPE_M3U), pipe_lines, "pipe/Kodi"),
        (Path(OUTPUT_VLC_M3U), vlc_lines, "VLC"),
    )

    if count_links:
        for path, lines, _label in output_sets:
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        for path, _lines, label in output_sets:
            if path.exists():
                print(f"⚠️ Không có link mới; giữ nguyên playlist {label}: {path.resolve()}")
            else:
                path.write_text("#EXTM3U\n", encoding="utf-8")
                print(f"⚠️ Đã tạo playlist {label} rỗng: {path.resolve()}")

    if count_links:
        m3u8_count = sum(
            1 for line in vlc_lines if line.startswith("http") and stream_kind(line) == "m3u8"
        )
        flv_count = sum(
            1 for line in vlc_lines if line.startswith("http") and stream_kind(line) == "flv"
        )
        print(f"📊 Playlist: M3U8={m3u8_count} | FLV={flv_count}")
        print(
            "📡 Trạng thái: "
            f"verified={playability_counts.get('verified', 0)} | "
            f"pending={playability_counts.get('upcoming-pending', 0)} | "
            f"khác={sum(value for key, value in playability_counts.items() if key not in {'verified', 'upcoming-pending'})}"
        )
        group_summary = " | ".join(
            f"{group}={group_stream_counts[group]}"
            for group in SPORT_GROUP_ORDER if group_stream_counts[group]
        )
        if group_summary:
            print(f"📂 Thư mục playlist: {group_summary}")
        print(f"📺 Mặc định Android/IPTV: {Path(OUTPUT_M3U).resolve()}")
        print(f"📺 Pipe/Kodi tùy chọn: {Path(OUTPUT_PIPE_M3U).resolve()}")
        print(f"📺 VLC: {Path(OUTPUT_VLC_M3U).resolve()}")

    return len(match_keys_with_streams), count_links


async def progress_heartbeat(tasks: list[asyncio.Task[Any]], total: int) -> None:
    """In tiến trình đều đặn để GitHub Actions không đứng im trong lúc các tab đang chờ."""
    started = time.monotonic()
    try:
        while True:
            await asyncio.sleep(5)
            completed = sum(task.done() for task in tasks)
            if completed >= total:
                return
            active = min(CONCURRENCY_LIMIT, total - completed)
            waiting = max(0, total - completed - active)
            elapsed = int(time.monotonic() - started)
            print(
                f"⏳ Tiến trình realtime: xong {completed}/{total} | "
                f"đang/chờ tối đa {active}/{waiting} | đã chạy {elapsed}s",
                flush=True,
            )
    except asyncio.CancelledError:
        return


async def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True, write_through=True)
        except Exception:
            pass

    print(f"🥷 KHỞI ĐỘNG GÀ VÀNG STREAM SCANNER - {SCANNER_VERSION}", flush=True)
    print(
        "ℹ️ Lệnh test riêng một trận (chỉ là hướng dẫn, không tự chạy):\n"
        '   python sources/gavang.py "URL_TRẬN_GÀ_VÀNG"'
    )
    print(
        f"ℹ️ Chế độ quét: {'FULL toàn bộ thời gian' if FULL_SCAN else 'dừng sớm'} | "
        f"định dạng={','.join(STREAM_EXTENSIONS)} | chờ mỗi trận={STREAM_WAIT_SECONDS}s | "
        f"xác minh phát thật={'BẬT' if VERIFY_STREAMS else 'TẮT'} | "
        f"tối đa {MAX_VERIFY_CANDIDATES} ứng viên/{MAX_OUTPUT_STREAMS_PER_MATCH} mức chất lượng đầu ra | "
        f"lọc trận -{SCAN_PAST_MINUTES}/+{SCAN_FUTURE_MINUTES} phút | "
        f"giữ link pending trong {UPCOMING_KEEP_HOURS} giờ tới | "
        f"fallback chung chưa xác minh={'BẬT' if (ALLOW_UNVERIFIED_BROWSER_FALLBACK or KEEP_PREVIOUS_UNVERIFIED) else 'TẮT'} | "
        f"HTTP-first={'BẬT' if HYBRID_HTTP_FIRST else 'TẮT'} | "
        f"probe mọi stream_key thiếu giờ={'BẬT' if PROBE_UNKNOWN_STREAM_KEYS else 'TẮT'} | "
        f"giữ FLV dựng pending={'BẬT' if KEEP_DERIVED_PENDING else 'TẮT'} | "
        f"delta={'BẬT' if DELTA_SCAN_ENABLED else 'TẮT'} | "
        f"miền dự phòng={','.join(HOME_URLS)}"
    )

    direct_urls = [
        arg.strip() for arg in sys.argv[1:]
        if arg.strip().startswith(("http://", "https://"))
    ]
    delta_state = load_delta_state(STATE_PATH) if DELTA_SCAN_ENABLED and not direct_urls else {}
    if delta_state:
        print(f"ℹ️ Delta state: đã nạp {len(delta_state)} trận; chỉ quét lại khi đến next_scan_at.", flush=True)
    previous_streams_by_match = load_previous_playlist_streams()
    if previous_streams_by_match:
        print(
            f"ℹ️ Đã nạp playlist cũ của {len(previous_streams_by_match)} trận làm ứng viên xác minh lại; link thất bại sẽ không được xuất.",
            flush=True,
        )

    async with async_playwright() as playwright:
        launch_options: dict[str, Any] = {
            "headless": HEADLESS,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--mute-audio",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
            ],
        }
        configured_executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
        if configured_executable:
            executable_path = Path(configured_executable)
            if executable_path.is_file():
                launch_options["executable_path"] = str(executable_path)
                print(f"ℹ️ Dùng Chromium hệ thống: {executable_path}", flush=True)
            else:
                print(
                    f"⚠️ PLAYWRIGHT_CHROMIUM_EXECUTABLE không tồn tại: {executable_path}; "
                    "quay về Chromium do Playwright quản lý.",
                    flush=True,
                )
        browser = await playwright.chromium.launch(**launch_options)
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=UA,
            locale="vi-VN",
            timezone_id="Asia/Ho_Chi_Minh",
            ignore_https_errors=True,
            service_workers="block",
            extra_http_headers={
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
            },
        )

        if direct_urls:
            links = []
            for url in direct_urls:
                match_name, _, _ = derive_match_info(url)
                links.append({
                    "url": url,
                    "raw_title": match_name,
                    "raw_time": "",
                    "logo": "",
                    "team_logos": [],
                    "logo_candidates": [],
                    "source_logo": default_gavang_source_logo(url),
                    "source_logo_candidates": [],
                    "sport_hint": "",
                    "sport_group": classify_sport(match_name, url),
                })
            print(f"✅ Chế độ test trực tiếp: {len(links)} URL.")
        else:
            links = await collect_home_links_with_failover(context)
            links, window_stats = filter_links_by_scan_window(links)
            print_scan_window_summary(window_stats)
            if DELTA_SCAN_ENABLED:
                due_links: list[dict[str, Any]] = []
                skipped_delta = 0
                for item in links:
                    key = match_id_from_url(item.get("url", ""))
                    due, reason = should_scan_now(
                        item, delta_state.get(key), near_minutes=DELTA_NEAR_MINUTES
                    )
                    item["delta_reason"] = reason
                    if due:
                        due_links.append(item)
                    else:
                        skipped_delta += 1
                links = due_links
                print(
                    f"🧠 Delta scan: đến lượt={len(links)} | hoãn={skipped_delta} | "
                    f"ngưỡng gần giờ={DELTA_NEAR_MINUTES} phút",
                    flush=True,
                )

        if not links:
            print("❌ Không tìm thấy link trận/phòng nào.")
            write_outputs([])
            await context.close()
            await browser.close()
            return

        for match in links:
            match_id = match_id_from_url(match.get("url", ""))
            match["_previous_streams"] = list(previous_streams_by_match.get(match_id, []))

        print(
            f"✅ Tìm thấy {len(links)} link trận/phòng. "
            f"Bắt đầu quét tối đa {CONCURRENCY_LIMIT} trang cùng lúc..."
        )
        semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
        total_links = len(links)
        tasks: list[asyncio.Task[dict[str, Any]]] = []
        for index, match in enumerate(links, start=1):
            match["_scan_index"] = index
            match["_scan_total"] = total_links
            tasks.append(asyncio.create_task(fetch_stream(context, match, semaphore)))

        heartbeat = asyncio.create_task(progress_heartbeat(tasks, total_links))
        results: list[dict[str, Any]] = []
        completed = 0
        try:
            for future in asyncio.as_completed(tasks):
                result = await future
                results.append(result)
                completed += 1
                found = len(result.get("streams") or [])
                print(
                    f"📈 Hoàn thành {completed}/{total_links}: "
                    f"[{result.get('sport_group', 'Khác')}] "
                    f"{result.get('match_name', '')[:70]} | stream={found}",
                    flush=True,
                )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

        remove_cross_match_shared_streams(results)

        pending_without_media = [
            row for row in results
            if isinstance(row.get("minutes_to_kickoff"), int)
            and 0 <= int(row.get("minutes_to_kickoff")) <= SCAN_FUTURE_MINUTES
            and not (row.get("streams") or [])
        ]
        if pending_without_media:
            print(
                f"ℹ️ Có {len(pending_without_media)} trận sắp đá trong cửa sổ nhưng trang chưa lộ "
                "URL M3U8/FLV; không đưa URL trang web vào M3U vì ứng dụng IPTV không phát được.",
                flush=True,
            )

        if DELTA_SCAN_ENABLED:
            update_state_from_results(delta_state, results, match_id_from_url)
            save_delta_state(STATE_PATH, delta_state, "gavang")
            print(f"💾 Đã cập nhật delta state: {STATE_PATH.resolve()}", flush=True)

        count_matches, count_links = write_outputs(results)

        if count_links:
            print(f"\n🎉 HOÀN TẤT: lấy được {count_links} link từ {count_matches} trận/phòng.")
            print(f"📺 Playlist mặc định: {Path(OUTPUT_M3U).resolve()}")
            print(f"📺 Playlist pipe/Kodi: {Path(OUTPUT_PIPE_M3U).resolve()}")
            print(f"📺 Playlist VLC: {Path(OUTPUT_VLC_M3U).resolve()}")
        else:
            print("\n❌ Không bắt được m3u8/flv nào.")
        print(f"🧾 Nhật ký chi tiết: {Path(OUTPUT_DEBUG).resolve()}")

        await context.close()
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
