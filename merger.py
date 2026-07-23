from __future__ import annotations

import json
import hashlib
import os
import re
import unicodedata
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

VERSION = "4.4.27-FAST-REGISTRY-PILOT"
TZ_VIETNAM = ZoneInfo("Asia/Ho_Chi_Minh")
ALLOWED_GROUPS = {"Bóng đá", "Bóng rổ", "Bóng chuyền", "Tennis", "Esports", "Khác"}
SOURCE_ORDER = {"chuoichien": 0, "luongson": 1, "gavang": 2, "xoilac": 3, "colatv": 4, "phaohoa": 5}
PLAYABILITY_RANK = {
    "verified": 4,
    "browser-observed": 3,
    "upcoming-pending": 2,
    "metadata-only": 1,
}
QUALITY_RANK = {"4K": 5, "FHD": 4, "HD": 3, "SD": 2, "UNKNOWN": 1}

SOURCE_WINDOW_ENV = {
    "chuoichien": ("SOCOLIVE_SCAN_PAST_MINUTES", "SOCOLIVE_SCAN_FUTURE_MINUTES"),
    "luongson": ("HYGENIE_SCAN_PAST_MINUTES", "HYGENIE_SCAN_FUTURE_MINUTES"),
    "gavang": ("GAVANG_SCAN_PAST_MINUTES", "GAVANG_SCAN_FUTURE_MINUTES"),
    "xoilac": ("XOILAC_SCAN_PAST_MINUTES", "XOILAC_SCAN_FUTURE_MINUTES"),
    "colatv": ("COLATV_SCAN_PAST_MINUTES", "COLATV_SCAN_FUTURE_MINUTES"),
    "phaohoa": ("PHAOHOA_SCAN_PAST_MINUTES", "PHAOHOA_SCAN_FUTURE_MINUTES"),
}



def read_env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def verified_only_enabled() -> bool:
    return read_env_bool("MULTI_VERIFIED_ONLY", True)

def read_window_minutes(name: str, default: int) -> int:
    try:
        return max(0, min(int(os.getenv(name, str(default))), 1440))
    except (TypeError, ValueError):
        return default

def source_scan_window(source_key: str) -> tuple[int, int]:
    past_env, future_env = SOURCE_WINDOW_ENV.get(source_key, ("MULTI_PENDING_PAST_MINUTES", "MULTI_SCAN_FUTURE_MINUTES"))
    return read_window_minutes(past_env, 150), read_window_minutes(future_env, 180)


@dataclass(slots=True)
class SourceFiles:
    key: str
    label: str
    universal: Path
    pipe: Path
    vlc: Path
    debug: Path
    fresh: bool = True
    returncode: int = 0


@dataclass(slots=True)
class M3UBlock:
    source_key: str
    source_label: str
    extinf: str
    lines: list[str]
    url_line: str
    canonical_url: str
    attributes: dict[str, str]
    display_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    score: int = 0
    match_key: str = ""
    quality: str = "UNKNOWN"
    kind: str = ""
    playability: str = ""
    kickoff: datetime | None = None


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_ascii(value: str) -> str:
    text = unicodedata.normalize("NFKD", clean_text(value).lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def canonical_stream_url(value: str) -> str:
    raw = clean_text(value)
    if "|" in raw:
        raw = raw.split("|", 1)[0]
    raw = unquote(raw).replace("\\/", "/")
    # Các tham số của iframe từng bị nối nhầm sau đuôi stream.
    raw = re.sub(r"(?i)(\.m3u8|\.flv)&(?:autoplay|isHome)=.*$", r"\1", raw)
    return raw


def stream_kind(url: str) -> str:
    path = urlparse(canonical_stream_url(url)).path.lower()
    if ".m3u8" in path:
        return "m3u8"
    if ".flv" in path:
        return "flv"
    return ""


def parse_attributes(extinf: str) -> dict[str, str]:
    return {key: value for key, value in re.findall(r'([\w-]+)="([^"]*)"', extinf)}


def parse_m3u(path: Path, source_key: str, source_label: str) -> list[M3UBlock]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    blocks: list[M3UBlock] = []
    current: list[str] = []
    extinf = ""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#EXTINF:"):
            current = [line]
            extinf = line
            continue
        if not current:
            continue
        current.append(line)
        if stripped.startswith(("http://", "https://")):
            display = extinf.split(",", 1)[1] if "," in extinf else ""
            blocks.append(
                M3UBlock(
                    source_key=source_key,
                    source_label=source_label,
                    extinf=extinf,
                    lines=list(current),
                    url_line=line,
                    canonical_url=canonical_stream_url(line),
                    attributes=parse_attributes(extinf),
                    display_name=clean_text(display),
                )
            )
            current = []
            extinf = ""
    return blocks




PHAOHOA_METADATA_HOSTS = {"phaohoa1.live", "www.phaohoa1.live"}
PHAOHOA_PLACEHOLDER_HOSTS = {"127.0.0.1", "localhost"}
PHAOHOA_PLACEHOLDER_PATH_PREFIX = "/__phaohoa_metadata__/"


def phaohoa_declared_page_url(block: M3UBlock) -> str:
    return canonical_stream_url(block.attributes.get("phaohoa-page-url", ""))


def is_valid_phaohoa_page_url(value: str) -> bool:
    parsed = urlparse(canonical_stream_url(value))
    if (parsed.hostname or "").lower() not in PHAOHOA_METADATA_HOSTS:
        return False
    return bool(re.search(r"/(?:truc-tiep|live|room)/", parsed.path, re.I))


def is_phaohoa_metadata_placeholder(block: M3UBlock) -> bool:
    """Chỉ cho phép placeholder loopback .m3u8 của Pháo Hoa làm mục lịch."""
    if block.source_key != "phaohoa":
        return False
    if clean_text(block.attributes.get("phaohoa-entry")).lower() != "metadata-only":
        return False
    parsed = urlparse(block.canonical_url)
    if (parsed.hostname or "").lower() not in PHAOHOA_PLACEHOLDER_HOSTS:
        return False
    if parsed.port not in {None, 9}:
        return False
    if not parsed.path.startswith(PHAOHOA_PLACEHOLDER_PATH_PREFIX):
        return False
    if not parsed.path.lower().endswith(".m3u8"):
        return False
    return is_valid_phaohoa_page_url(phaohoa_declared_page_url(block))

MULTISOURCE_PLACEHOLDER_PATH_PREFIX = "/__multisource_metadata__/"

def metadata_placeholder_page_url(block: M3UBlock) -> str:
    return canonical_stream_url(
        block.attributes.get("catalog-page-url")
        or block.attributes.get("phaohoa-page-url")
        or block.metadata.get("url")
        or block.metadata.get("final_url")
        or ""
    )

def is_metadata_placeholder(block: M3UBlock) -> bool:
    if is_phaohoa_metadata_placeholder(block):
        return True
    if clean_text(block.attributes.get("catalog-entry")).lower() != "metadata-only":
        return False
    parsed = urlparse(block.canonical_url)
    if (parsed.hostname or "").lower() not in PHAOHOA_PLACEHOLDER_HOSTS or parsed.port not in {None, 9}:
        return False
    expected = f"{MULTISOURCE_PLACEHOLDER_PATH_PREFIX}{block.source_key}/"
    return parsed.path.startswith(expected) and parsed.path.lower().endswith(".m3u8")

def _m3u_attr(value: Any) -> str:
    return clean_text(value).replace("&", "&amp;").replace('"', "'")

def _catalog_display_name(row: dict[str, Any], kickoff: datetime | None) -> str:
    name = _metadata_name(row) or "Trận chưa rõ tên"
    name = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", name).strip()
    blv = extract_blv(row, name)
    if kickoff:
        prefix = kickoff.strftime("[%H:%M %d/%m]")
    else:
        date_text = clean_text(row.get("date"))
        time_text = clean_text(row.get("time"))
        if time_text and date_text:
            prefix = f"[{time_text} {date_text}]"
        elif date_text:
            prefix = f"[CHƯA CÓ GIỜ {date_text}]"
        elif time_text:
            prefix = f"[{time_text} CHƯA RÕ NGÀY]"
        else:
            prefix = "[CHƯA CÓ LỊCH]"
    suffix = f" [BLV {blv}]" if blv else ""
    return f"{prefix} {name}{suffix}".strip()

def build_catalog_placeholder(source: SourceFiles, row: dict[str, Any], index: int, now: datetime) -> M3UBlock | None:
    name = _metadata_name(row)
    if not re.search(r"\bvs\b", name, re.I):
        return None
    kickoff = resolve_kickoff(row, now)
    page_url = canonical_stream_url(row.get("url") or row.get("final_url") or row.get("input_url") or "")
    stable = clean_text(row.get("match_id") or row.get("id") or page_url or f"{source.key}-{index}")
    digest = hashlib.sha1(stable.encode("utf-8", errors="ignore")).hexdigest()[:16]
    url = f"http://127.0.0.1:9{MULTISOURCE_PLACEHOLDER_PATH_PREFIX}{source.key}/{digest}.m3u8"
    display = _catalog_display_name(row, kickoff)
    logo = _first_valid_logo_from_row(row)
    attrs = {
        "tvg-id": f"{source.key}-catalog-{digest}",
        "tvg-name": display,
        "group-title": source.label,
        "catalog-entry": "metadata-only",
        "catalog-source": source.key,
        "catalog-page-url": page_url,
    }
    if logo:
        attrs["tvg-logo"] = logo
    attr_text = " ".join(f'{key}="{_m3u_attr(value)}"' for key, value in attrs.items())
    extinf = f"#EXTINF:-1 {attr_text},{display}"
    meta = dict(row)
    meta.setdefault("listed_in_playlist", True)
    meta.setdefault("playability", "metadata-only")
    if kickoff:
        meta["kickoff_iso"] = kickoff.isoformat()
    blv = extract_blv(meta, display)
    return M3UBlock(
        source_key=source.key, source_label=source.label, extinf=extinf,
        lines=[extinf, url], url_line=url, canonical_url=url, attributes=attrs,
        display_name=display, metadata=meta, score=PLAYABILITY_RANK["metadata-only"] * 100 + (5 if source.fresh else 0),
        match_key=f"{normalize_match_name(name)}|{blv}", quality="UNKNOWN",
        kind="placeholder-m3u8", playability="metadata-only", kickoff=kickoff,
    )

def _parse_datetime_value(value: Any) -> datetime | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TZ_VIETNAM)
        return parsed.astimezone(TZ_VIETNAM)
    except ValueError:
        return None


def resolve_kickoff(row: dict[str, Any], now: datetime) -> datetime | None:
    direct = _parse_datetime_value(row.get("kickoff_iso"))
    if direct:
        return direct
    date_text = clean_text(row.get("date"))
    time_text = clean_text(row.get("time"))
    time_match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", time_text)
    if not time_match:
        return None
    hour, minute = map(int, time_match.groups())
    date_candidates: list[datetime] = []
    if date_text:
        # Không dùng strptime với định dạng chỉ có ngày/tháng vì Python 3.15 sẽ siết
        # hành vi năm mặc định và hiện đã phát cảnh báo DeprecationWarning.
        short_date = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})", date_text)
        if short_date:
            day, month = map(int, short_date.groups())
            try:
                date_candidates.append(datetime(now.year, month, day, hour, minute, tzinfo=TZ_VIETNAM))
            except ValueError:
                pass
        else:
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(date_text, fmt)
                    date_candidates.append(datetime(parsed.year, parsed.month, parsed.day, hour, minute, tzinfo=TZ_VIETNAM))
                    break
                except ValueError:
                    continue
    if not date_candidates:
        for day_offset in (-1, 0, 1):
            day = (now + timedelta(days=day_offset)).date()
            date_candidates.append(datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ_VIETNAM))
    return min(date_candidates, key=lambda dt: abs((dt - now).total_seconds()))


def normalize_quality(value: Any, display_name: str, url: str) -> str:
    text = " ".join((clean_text(value), display_name, url)).lower()
    if re.search(r"\b(4k|uhd|2160)\b", text):
        return "4K"
    if re.search(r"\b(fhd|full\s*hd|1080)\b", text) or re.search(r"(?i)(?:hd)(?:/playlist\.m3u8|\.flv)(?:$|\?)", url):
        return "FHD"
    if re.search(r"\b(hd|720)\b", text):
        return "HD"
    if re.search(r"\b(sd|480|360)\b", text):
        return "SD"
    return "UNKNOWN"


def normalize_match_name(value: str) -> str:
    text = clean_text(value)
    # Có thể có đồng thời [CHỜ PHÁT] và [HH:MM DD/MM] ở đầu.
    text = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", text)
    text = re.sub(r"\s*\[(?:CHỜ PHÁT\s+)?(?:4K|FHD|HD|SD)?\s*(?:M3U8|FLV)\]\s*$", "", text, flags=re.I)
    text = re.sub(r"\s*\[BLV\s+[^\]]+\]", "", text, flags=re.I)
    match = re.search(r"(.+?)\s+vs\s+(.+?)(?:\s+-\s+|$)", text, flags=re.I)
    if match:
        return f"{normalize_ascii(match.group(1))} vs {normalize_ascii(match.group(2))}"
    return normalize_ascii(text)



GAVANG_KEY_NOISE = {
    "ausffa", "auscup", "kork1", "kork2", "chnfa", "chnfacup", "finveik",
    "argcopa", "argcup", "c1qual", "c2qual", "c3qual", "uclqual", "uefaqual",
    "lbnprem", "uzbsuper", "uzbpro", "ligaprosa", "jpnj1", "jpnj2",
    "thaprem", "viecup", "affw", "mls", "kazdiv1", "norelite",
}

GAVANG_KEY_ALIASES = {
    "camw": ["cambodia"],
    "sinw": ["singapore"],
    "sydnet58": ["sydney"],
    "mariners": ["mariners"],
    "buncheon": ["bucheon"],
    "cincinati": ["cincinnati"],
    "lagalaxy": ["galaxy"],
    "stlouis": ["louis"],
    "tot": ["tottenham"],
    "mkdons": ["dons"],
    "bodo": ["bodo"],
    "hamkam": ["ham", "kam"],
}


def gavang_key_tokens_from_stream(url: str) -> list[str]:
    parsed = urlparse(canonical_stream_url(url))
    parts = [part for part in parsed.path.split("/") if part]
    stem = Path(parsed.path).stem.lower()
    if stem == "index" and len(parts) >= 2:
        stem = parts[-2].lower()
    tokens = [token for token in re.split(r"[^a-z0-9]+", stem) if token]
    while tokens and tokens[-1] in GAVANG_KEY_NOISE:
        tokens = tokens[:-1]
    expanded: list[str] = []
    for token in tokens:
        if token in {"vs", "live", "stream"}:
            continue
        values = GAVANG_KEY_ALIASES.get(token, [token])
        expanded.extend(value for value in values if len(value) >= 3)
    return expanded


def _metadata_name(row: dict[str, Any], display_name: str = "") -> str:
    return clean_text(row.get("match_name") or row.get("raw_title") or display_name)


def _token_similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if min(len(left), len(right)) >= 5 and (left.startswith(right) or right.startswith(left)):
        return 0.94
    return SequenceMatcher(None, left, right).ratio()


def _candidate_key_score(key_tokens: list[str], candidate_name: str) -> tuple[int, int, float]:
    candidate_tokens = [token for token in normalize_ascii(candidate_name).split() if len(token) >= 3]
    unused = set(range(len(candidate_tokens)))
    matched = 0
    similarity_total = 0.0
    for key_token in key_tokens:
        ranked = sorted(
            (( _token_similarity(key_token, candidate_tokens[index]), index) for index in unused),
            reverse=True,
        )
        if not ranked or ranked[0][0] < 0.82:
            continue
        similarity, index = ranked[0]
        unused.remove(index)
        matched += 1
        similarity_total += similarity
    coverage = matched / len(key_tokens) if key_tokens else 0.0
    average_similarity = similarity_total / matched if matched else 0.0
    # Cho phép lỗi chính tả nhẹ như buncheon/bucheon nhưng vẫn yêu cầu hai phía đội khớp.
    score = matched * 100 + int(coverage * 50) + int(average_similarity * 25) + min(len(candidate_name), 160) // 10
    return score, matched, coverage


INVALID_LOGO_MARKERS = ("[object object]", "object object", "undefined", "null")
DEFAULT_GAVANG_LOGO = os.getenv("GAVANG_SOURCE_LOGO_URL", "https://smorf.io/favicon.ico").strip()


def valid_logo_url(value: Any) -> bool:
    text = clean_text(value)
    if not text or any(marker in text.lower() for marker in INVALID_LOGO_MARKERS):
        return False
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _set_block_logo(block: M3UBlock, value: str, source: str) -> bool:
    logo = clean_text(value).replace('"', "'")
    if not valid_logo_url(logo):
        return False
    if re.search(r'(?<![\w-])tvg-logo="[^"]*"', block.extinf):
        block.extinf = re.sub(
            r'(?<![\w-])tvg-logo="[^"]*"', f'tvg-logo="{logo}"', block.extinf, count=1
        )
    else:
        head, sep, tail = block.extinf.partition(",")
        head = f'{head} tvg-logo="{logo}"'
        block.extinf = f"{head},{tail}" if sep else head
    block.attributes["tvg-logo"] = logo
    block.metadata["logo"] = logo
    block.metadata["logo_source"] = source
    block.metadata["logo_is_fallback"] = source == "gavang-source-fallback"
    for index, line in enumerate(block.lines):
        if line.strip().startswith("#EXTINF:"):
            block.lines[index] = block.extinf
            break
    return True


def _replace_extinf_display(extinf: str, display_base: str, display_full: str) -> str:
    safe_base = clean_text(display_base).replace('"', "'")
    safe_full = clean_text(display_full).replace("\r", " ").replace("\n", " ")
    if re.search(r'(?<![\w-])tvg-name="[^"]*"', extinf):
        extinf = re.sub(r'(?<![\w-])tvg-name="[^"]*"', f'tvg-name="{safe_base}"', extinf, count=1)
    head, sep, _old_display = extinf.partition(",")
    return f"{head},{safe_full}" if sep else f"{extinf},{safe_full}"


def _apply_block_display_metadata(
    block: M3UBlock,
    *,
    match_name: str,
    kickoff: datetime | None,
    date_text: str = "",
    time_text: str = "",
) -> None:
    own_blv = clean_text(block.metadata.get("blv"))
    if kickoff:
        time_text = kickoff.strftime("%H:%M")
        date_text = kickoff.strftime("%d/%m")
    is_pending = (
        block.playability == "upcoming-pending"
        or bool(block.metadata.get("derived_pending"))
        or bool(re.search(r"\[CHỜ PHÁT(?:\s+[^\]]+)?\]", block.display_name, re.I))
    )
    if time_text and date_text:
        schedule_label = f"{time_text} {date_text}"
    elif date_text:
        schedule_label = f"CHƯA CÓ GIỜ {date_text}"
    elif time_text:
        schedule_label = f"{time_text} CHƯA RÕ NGÀY"
    elif is_pending:
        schedule_label = "CHƯA CÓ LỊCH"
    else:
        schedule_label = ""
    display_base = f"[{schedule_label}] {match_name}" if schedule_label else match_name
    if own_blv and own_blv.lower() not in display_base.lower():
        display_base += f" [BLV {own_blv}]"
    suffix_match = re.search(
        r"(\s*\[(?:CHỜ PHÁT\s+)?(?:(?:4K|FHD|HD|SD)\s+)?(?:M3U8|FLV)\])\s*$",
        block.display_name,
        flags=re.I,
    )
    suffix = suffix_match.group(1).strip() if suffix_match else (f"[{block.quality} {block.kind.upper()}]" if block.quality != "UNKNOWN" else f"[{block.kind.upper()}]")
    suffix = re.sub(r"^\[CHỜ PHÁT\s+", "[", suffix, flags=re.I)
    display_full = f"{display_base} {suffix}".strip()
    block.display_name = display_full
    block.extinf = _replace_extinf_display(block.extinf, display_base, display_full)
    for index, line in enumerate(block.lines):
        if line.strip().startswith("#EXTINF:"):
            block.lines[index] = block.extinf
            break



def _first_valid_logo_from_row(row: dict[str, Any]) -> str:
    values: list[Any] = [
        row.get("logo"), row.get("home_logo"), row.get("away_logo"),
        row.get("source_logo"), row.get("gavang_logo"),
    ]
    values.extend(row.get("team_logos") or [])
    values.extend(row.get("logo_candidates") or [])
    for value in values:
        if isinstance(value, dict):
            value = value.get("url") or value.get("src") or value.get("contentUrl")
        fixed = clean_text(value)
        if valid_logo_url(fixed):
            return fixed
    return ""


def load_debug_metadata_references(source: SourceFiles, now: datetime) -> list[M3UBlock]:
    """Tạo record tham chiếu từ toàn bộ debug row, kể cả trận chưa có stream.

    Trước đây merger chỉ đối chiếu Gà Vàng với các block đã vào playlist nguồn.
    Một trận sắp đá thường có tên/giờ/logo trong debug Chuối Chiên/Lương Sơn nhưng
    chưa có media nên bị bỏ khỏi tập tham chiếu. Đây là nguyên nhân audit ghi
    ``enriched=0`` dù nguồn khác đã nhìn thấy lịch trận.
    """
    if source.key == "gavang" or not source.debug.exists():
        return []
    try:
        payload = json.loads(source.debug.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload if isinstance(payload, list) else payload.get("results", []) if isinstance(payload, dict) else []
    output: list[M3UBlock] = []
    seen: set[tuple[str, str]] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        name = _metadata_name(row)
        if not re.search(r"\bvs\b", name, re.I):
            continue
        kickoff = resolve_kickoff(row, now)
        date_text = clean_text(row.get("date"))
        time_text = clean_text(row.get("time"))
        key = (normalize_match_name(name), kickoff.isoformat() if kickoff else f"{date_text}|{time_text}")
        if key in seen:
            continue
        seen.add(key)
        logo = _first_valid_logo_from_row(row)
        metadata = dict(row)
        if kickoff:
            metadata["kickoff_iso"] = kickoff.isoformat()
        attrs = {"tvg-logo": logo} if logo else {}
        output.append(M3UBlock(
            source_key=source.key,
            source_label=source.label,
            extinf="",
            lines=[],
            url_line="",
            canonical_url=f"debug://{source.key}/{index}",
            attributes=attrs,
            display_name=name,
            metadata=metadata,
            kickoff=kickoff,
            kind="",
            playability="metadata-only",
        ))
    return output

def enrich_gavang_metadata_from_other_sources(
    blocks: list[M3UBlock],
    metadata_references: list[M3UBlock] | None = None,
) -> dict[str, int]:
    """Best-effort metadata bridge cho Gà Vàng; không bao giờ loại stream.

    Stream key chỉ được dùng để tìm tên/giờ tương ứng ở Chuối Chiên/Lương Sơn.
    Nếu không đủ chắc chắn, giữ nguyên link và metadata hiện có, đồng thời ghi warning.
    """
    reference_pool = list(blocks) + list(metadata_references or [])
    reference = [
        item for item in reference_pool
        if item.source_key != "gavang" and _metadata_name(item.metadata, item.display_name)
    ]
    stats = {"enriched": 0, "warn_only": 0, "already_good": 0}
    for block in blocks:
        if block.source_key != "gavang":
            continue
        key_tokens = gavang_key_tokens_from_stream(block.canonical_url)
        current_name = _metadata_name(block.metadata, block.display_name)
        current_score, current_matches, _ = _candidate_key_score(key_tokens, current_name)
        has_time = bool(block.kickoff or (clean_text(block.metadata.get("time")) and clean_text(block.metadata.get("date"))))
        if current_matches >= min(2, len(key_tokens)) and has_time:
            block.metadata["metadata_audit"] = "ok"
            stats["already_good"] += 1
            continue

        candidates: list[tuple[int, M3UBlock, int, float]] = []
        for candidate in reference:
            candidate_name = _metadata_name(candidate.metadata, candidate.display_name)
            score, matched, coverage = _candidate_key_score(key_tokens, candidate_name)
            if candidate.kickoff:
                score += 35
            if candidate.source_key == "luongson":
                score += 3
            candidates.append((score, candidate, matched, coverage))
        candidates.sort(key=lambda row: row[0], reverse=True)
        best = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        required = min(2, len(key_tokens)) if key_tokens else 99
        confident = bool(
            best and best[2] >= required and best[3] >= 0.5 and
            (not second or best[0] - second[0] >= 5 or normalize_match_name(_metadata_name(best[1].metadata, best[1].display_name)) == normalize_match_name(_metadata_name(second[1].metadata, second[1].display_name)))
        )
        if not confident:
            block.metadata["metadata_audit"] = "warn-only"
            block.metadata["metadata_warning"] = (
                "Không tìm được lịch/tên đối chiếu đủ tin cậy từ nguồn khác; stream verified/pending vẫn được giữ"
            )
            block.metadata["stream_key_tokens"] = key_tokens
            # Chuẩn hóa tên hiển thị kể cả khi chưa làm giàu được metadata:
            # Lịch thiếu vẫn được ghi rõ; trạng thái pending chỉ giữ trong debug, không chèn vào tên kênh.
            _apply_block_display_metadata(
                block,
                match_name=current_name,
                kickoff=block.kickoff,
                date_text=clean_text(block.metadata.get("date")),
                time_text=clean_text(block.metadata.get("time")),
            )
            stats["warn_only"] += 1
            continue

        candidate = best[1]
        candidate_name = _metadata_name(candidate.metadata, candidate.display_name)
        kickoff = candidate.kickoff
        date_text = clean_text(candidate.metadata.get("date"))
        time_text = clean_text(candidate.metadata.get("time"))
        # Chỉ nâng cấp tên nếu tên hiện tại thiếu hoặc trái hẳn stream key; không lấy BLV nguồn khác.
        chosen_name = current_name
        if current_matches < required or len(candidate_name) > len(current_name):
            chosen_name = candidate_name
        block.metadata["match_name"] = chosen_name
        if kickoff:
            block.metadata["kickoff_iso"] = kickoff.isoformat()
            block.metadata["time"] = kickoff.strftime("%H:%M")
            block.metadata["date"] = kickoff.strftime("%d/%m")
            block.kickoff = kickoff
        elif time_text:
            block.metadata["time"] = time_text
            block.metadata["date"] = date_text
        block.metadata["metadata_audit"] = "enriched-soft"
        block.metadata["metadata_enriched_from"] = candidate.source_key
        block.metadata["metadata_key_matches"] = best[2]
        block.metadata["stream_key_tokens"] = key_tokens
        _apply_block_display_metadata(
            block,
            match_name=chosen_name,
            kickoff=kickoff,
            date_text=date_text,
            time_text=time_text,
        )
        own_blv = extract_blv(block.metadata, block.display_name)
        block.match_key = f"{normalize_match_name(chosen_name)}|{own_blv}"
        stats["enriched"] += 1
    return stats


def enrich_gavang_logos_from_other_sources(
    blocks: list[M3UBlock],
    metadata_references: list[M3UBlock] | None = None,
) -> dict[str, int]:
    """Ưu tiên logo đội từ nguồn khác khi khớp đủ chắc; nếu không có, giữ logo nguồn."""
    reference_pool = list(blocks) + list(metadata_references or [])
    reference = [
        item for item in reference_pool
        if item.source_key != "gavang" and valid_logo_url(item.attributes.get("tvg-logo"))
    ]
    stats = {"team_logo": 0, "source_fallback": 0, "repaired_invalid": 0}
    for block in blocks:
        if block.source_key != "gavang":
            continue
        current = block.attributes.get("tvg-logo", "")
        current_invalid = not valid_logo_url(current)
        current_fallback = bool(block.metadata.get("logo_is_fallback")) or current.rstrip("/").endswith("favicon.ico")
        key_tokens = gavang_key_tokens_from_stream(block.canonical_url)
        candidates: list[tuple[int, M3UBlock, int, float]] = []
        for candidate in reference:
            candidate_name = _metadata_name(candidate.metadata, candidate.display_name)
            score, matched, coverage = _candidate_key_score(key_tokens, candidate_name)
            if candidate.kickoff:
                score += 20
            candidates.append((score, candidate, matched, coverage))
        candidates.sort(key=lambda row: row[0], reverse=True)
        best = candidates[0] if candidates else None
        second = candidates[1] if len(candidates) > 1 else None
        required = min(2, len(key_tokens)) if key_tokens else 99
        confident = bool(
            best and best[2] >= required and best[3] >= 0.5 and
            (not second or best[0] - second[0] >= 5 or normalize_match_name(_metadata_name(best[1].metadata, best[1].display_name)) == normalize_match_name(_metadata_name(second[1].metadata, second[1].display_name)))
        )
        if confident and (current_invalid or current_fallback):
            candidate_logo = best[1].attributes.get("tvg-logo", "")
            if _set_block_logo(block, candidate_logo, f"team-from-{best[1].source_key}"):
                block.metadata["logo_enriched_from"] = best[1].source_key
                stats["team_logo"] += 1
                if current_invalid:
                    stats["repaired_invalid"] += 1
                continue
        if current_invalid:
            _set_block_logo(block, DEFAULT_GAVANG_LOGO, "gavang-source-fallback")
            stats["repaired_invalid"] += 1
        stats["source_fallback"] += 1
    return stats


def extract_blv(row: dict[str, Any], display_name: str) -> str:
    value = clean_text(row.get("blv"))
    if value:
        return normalize_ascii(value)
    match = re.search(r"\[BLV\s+([^\]]+)\]", display_name, flags=re.I)
    return normalize_ascii(match.group(1)) if match else ""


def build_debug_index(debug_path: Path, now: datetime) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    if not debug_path.exists():
        return {}, []
    try:
        payload = json.loads(debug_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}, []
    rows = payload if isinstance(payload, list) else payload.get("results", []) if isinstance(payload, dict) else []
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        kickoff = resolve_kickoff(row, now)
        for stream in row.get("streams") or []:
            if not isinstance(stream, dict):
                continue
            url = canonical_stream_url(stream.get("url", ""))
            if not url:
                continue
            merged = dict(row)
            merged.update(stream)
            merged["_kickoff"] = kickoff
            current = index.get(url)
            if current is None or PLAYABILITY_RANK.get(clean_text(merged.get("playability")), 0) > PLAYABILITY_RANK.get(clean_text(current.get("playability")), 0):
                index[url] = merged
    return index, rows


def enrich_blocks(source: SourceFiles, blocks: list[M3UBlock], now: datetime) -> tuple[list[M3UBlock], int]:
    debug_index, rows = build_debug_index(source.debug, now)
    phaohoa_page_index: dict[str, dict[str, Any]] = {}
    if source.key == "phaohoa":
        for row in rows:
            if not isinstance(row, dict):
                continue
            page_url = canonical_stream_url(row.get("playlist_page_url") or row.get("url") or "")
            if not page_url:
                continue
            merged = dict(row)
            merged["_kickoff"] = resolve_kickoff(row, now)
            merged.setdefault("playability", "metadata-only")
            phaohoa_page_index[page_url] = merged

    for block in blocks:
        placeholder = is_metadata_placeholder(block)
        meta = dict(debug_index.get(block.canonical_url, {}))
        if placeholder and not meta:
            meta = dict(phaohoa_page_index.get(phaohoa_declared_page_url(block), {}))
        block.metadata = meta
        block.playability = "metadata-only" if placeholder else clean_text(meta.get("playability"))
        block.kickoff = meta.get("_kickoff") if isinstance(meta.get("_kickoff"), datetime) else None
        block.quality = "UNKNOWN" if placeholder else normalize_quality(meta.get("quality"), block.display_name, block.canonical_url)
        block.kind = "placeholder-m3u8" if placeholder else stream_kind(block.canonical_url)
        match_name = clean_text(meta.get("match_name") or meta.get("raw_title") or block.display_name)
        blv = extract_blv(meta, block.display_name)
        block.match_key = f"{normalize_match_name(match_name)}|{blv}"
        observed = bool(meta.get("observed_active"))
        status = meta.get("http_status") or meta.get("status")
        status_bonus = 10 if status in {200, 206, "200", "206"} else 0
        block.score = (
            PLAYABILITY_RANK.get(block.playability, 0) * 100
            + QUALITY_RANK.get(block.quality, 1) * 10
            + (6 if block.kind == "m3u8" else 3 if block.kind == "flv" else 0)
            + (8 if observed else 0)
            + status_bonus
            + (5 if source.fresh else 0)
        )
    return blocks, len(rows)


def source_window_delta(block: M3UBlock, now: datetime) -> float | None:
    raw_delta = block.metadata.get("minutes_to_kickoff")
    if isinstance(raw_delta, (int, float)):
        return float(raw_delta)
    if isinstance(raw_delta, str):
        try:
            return float(raw_delta)
        except ValueError:
            pass
    if block.kickoff:
        return (block.kickoff - now).total_seconds() / 60
    return None

def block_within_source_window(block: M3UBlock, now: datetime, *, require_known_time: bool = False) -> bool:
    """Chỉ giữ kênh có lịch nếu nằm trong cửa sổ -past/+future của chính nguồn đó."""
    minutes = source_window_delta(block, now)
    if minutes is None:
        return not require_known_time
    past, future = source_scan_window(block.source_key)
    return -past <= minutes <= future

def metadata_placeholder_within_source_window(block: M3UBlock, now: datetime) -> bool:
    """Chỉ giữ mục lịch metadata-only nếu trận nằm trong cửa sổ của chính nguồn đó."""
    return block_within_source_window(block, now, require_known_time=True)

def _status_ok_for_verified(block: M3UBlock) -> bool:
    status = block.metadata.get("http_status")
    if status is None:
        status = block.metadata.get("status")
    if status in {401, 403, 404, 410, 429, "401", "403", "404", "410", "429"}:
        return False
    # Adapter đã gắn playability=verified sau probe riêng của nguồn; thiếu status trong debug
    # không được biến một stream verified thành pending/rỗng ở tầng merger.
    if block.playability == "verified":
        return True
    if status in {200, 206, "200", "206"}:
        return True
    return bool(block.metadata.get("verified") or block.metadata.get("probe_ok") or block.metadata.get("observed_active"))


def is_candidate_allowed(block: M3UBlock, now: datetime, upcoming_hours: int) -> bool:
    verified_only = verified_only_enabled()
    if block.playability == "metadata-only":
        if verified_only:
            return False
        return (
            is_metadata_placeholder(block)
            and bool(block.metadata.get("listed_in_playlist", True))
            and metadata_placeholder_within_source_window(block, now)
        )
    if block.playability == "verified":
        return _status_ok_for_verified(block) and block_within_source_window(block, now)
    if block.playability == "browser-observed":
        return (
            bool(block.metadata.get("observed_active"))
            and _status_ok_for_verified(block)
            and block_within_source_window(block, now)
        )
    if verified_only:
        return False
    if block.playability == "upcoming-pending" and block.kickoff:
        return block_within_source_window(block, now, require_known_time=True)
    if (
        block.source_key == "gavang"
        and block.playability == "upcoming-pending"
        and block.metadata.get("derived_pending")
        and os.getenv("MULTI_KEEP_GAVANG_UNKNOWN_PENDING", "1").strip().lower()
        not in {"0", "false", "no", "off"}
        and clean_text(block.metadata.get("scan_window_reason"))
        in {"unknown-time-live", "unknown-time-derived-probe"}
    ):
        return True
    return False


def choose_candidates(blocks: Iterable[M3UBlock], now: datetime, max_per_match: int, upcoming_hours: int) -> tuple[list[M3UBlock], list[dict[str, Any]]]:
    best_by_url: dict[str, M3UBlock] = {}
    dropped: list[dict[str, Any]] = []
    for block in blocks:
        placeholder = is_metadata_placeholder(block)
        if not block.canonical_url or (block.kind not in {"m3u8", "flv"} and not placeholder):
            dropped.append({"url": block.canonical_url, "reason": "not-stream", "source": block.source_key})
            continue
        if not is_candidate_allowed(block, now, upcoming_hours):
            dropped.append({"url": block.canonical_url, "reason": "not-verified-or-not-upcoming", "source": block.source_key})
            continue
        previous = best_by_url.get(block.canonical_url)
        if previous is None or block.score > previous.score:
            best_by_url[block.canonical_url] = block

    grouped: dict[str, list[M3UBlock]] = {}
    for block in best_by_url.values():
        grouped.setdefault(block.match_key or normalize_match_name(block.display_name), []).append(block)

    selected: list[M3UBlock] = []
    for match_key, items in grouped.items():
        real_items = [item for item in items if not is_metadata_placeholder(item)]
        if real_items:
            for item in items:
                if is_metadata_placeholder(item):
                    dropped.append({
                        "url": item.canonical_url,
                        "reason": "stream-replaces-metadata-only",
                        "source": item.source_key,
                        "match_key": match_key,
                    })
            items = real_items
        items.sort(key=lambda item: (-item.score, item.source_key, item.canonical_url))
        qualities: set[str] = set()
        chosen: list[M3UBlock] = []
        for item in items:
            qkey = item.quality
            if qkey in qualities:
                dropped.append({"url": item.canonical_url, "reason": f"duplicate-quality-{qkey}", "source": item.source_key, "match_key": match_key})
                continue
            chosen.append(item)
            qualities.add(qkey)
            if len(chosen) >= max_per_match:
                break
        for item in items:
            if item not in chosen and not any(row.get("url") == item.canonical_url for row in dropped):
                dropped.append({"url": item.canonical_url, "reason": "per-match-cap", "source": item.source_key, "match_key": match_key})
        selected.extend(chosen)

    selected.sort(
        key=lambda item: (
            SOURCE_ORDER.get(item.source_key, 999),
            item.kickoff or datetime.max.replace(tzinfo=TZ_VIETNAM),
            normalize_match_name(item.display_name),
            -item.score,
        )
    )
    return selected, dropped


def _block_map(path: Path, source: SourceFiles) -> dict[str, M3UBlock]:
    return {block.canonical_url: block for block in parse_m3u(path, source.key, source.label)}


def _source_group_extinf(extinf: str, source_label: str) -> str:
    """Gắn thư mục ảo theo nguồn cho một kênh trong playlist tổng."""
    safe_label = clean_text(source_label).replace('"', "'")
    replacement = f'group-title="{safe_label}"'
    if re.search(r'(?<![\w-])group-title="[^"]*"', extinf):
        return re.sub(r'(?<![\w-])group-title="[^"]*"', replacement, extinf, count=1)
    head, separator, display = extinf.partition(",")
    if not separator:
        return f"{extinf} {replacement}"
    spacer = "" if head.endswith(" ") else " "
    return f"{head}{spacer}{replacement},{display}"


def _render_source_grouped_lines(block: M3UBlock) -> list[str]:
    rendered = list(block.lines)
    for index, line in enumerate(rendered):
        if line.strip().startswith("#EXTINF:"):
            rendered[index] = _source_group_extinf(line, block.source_label)
            break
    return rendered


SOURCE_LABEL_TO_KEY = {
    "Chuối Chiên": "chuoichien",
    "Lương Sơn": "luongson",
    "Gà Vàng": "gavang",
    "Xôi Lạc": "xoilac",
    "ColaTV": "colatv",
    "Pháo Hoa TV": "phaohoa",
}
SOURCE_KEY_TO_LABEL = {value: key for key, value in SOURCE_LABEL_TO_KEY.items()}


def _previous_debug_payload(path: Path) -> tuple[dict[str, dict[str, Any]], datetime | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, None
    generated = _parse_datetime_value(payload.get("generated_at")) if isinstance(payload, dict) else None
    index: dict[str, dict[str, Any]] = {}
    if isinstance(payload, dict):
        for row in payload.get("channels", []):
            if not isinstance(row, dict):
                continue
            url = canonical_stream_url(row.get("url", ""))
            if url:
                index[url] = dict(row)
    return index, generated


def _parse_previous_playlist(root: Path, now: datetime) -> list[M3UBlock]:
    playlist = root / "all_live.m3u"
    if not playlist.exists():
        return []
    debug_index, generated_at = _previous_debug_payload(root / "all_live_debug.json")
    raw = parse_m3u(playlist, "previous", "Previous")
    output: list[M3UBlock] = []
    for block in raw:
        if is_metadata_placeholder(block) or stream_kind(block.canonical_url) not in {"m3u8", "flv"}:
            continue
        meta = dict(debug_index.get(block.canonical_url, {}))
        source_key = clean_text(meta.get("source")) or SOURCE_LABEL_TO_KEY.get(clean_text(block.attributes.get("group-title")), "")
        if source_key not in SOURCE_ORDER:
            continue
        prior_playability = clean_text(meta.get("playability"))
        if prior_playability not in {"verified", "browser-observed"}:
            continue
        block.source_key = source_key
        block.source_label = SOURCE_KEY_TO_LABEL[source_key]
        block.metadata = meta
        block.metadata["prior_generated_at"] = generated_at.isoformat() if generated_at else ""
        block.playability = "verified"
        block.kind = stream_kind(block.canonical_url)
        block.quality = normalize_quality(meta.get("quality"), block.display_name, block.canonical_url)
        block.kickoff = _parse_datetime_value(meta.get("kickoff_iso"))
        block.match_key = clean_text(meta.get("match_key")) or f"{normalize_match_name(block.display_name)}|{extract_blv(meta, block.display_name)}"
        block.score = PLAYABILITY_RANK["verified"] * 100 + QUALITY_RANK.get(block.quality, 1) * 10 - 25
        output.append(block)
    return output


def _signed_url_not_expired(url: str, min_seconds: int = 60) -> bool:
    query = parse_qs(urlparse(url).query)
    raw = (query.get("wsABSTime") or query.get("expires") or query.get("expire") or [""])[0]
    if not raw:
        return True
    try:
        value = int(raw, 16 if re.search(r"[a-f]", str(raw), re.I) else 10)
    except (TypeError, ValueError):
        return True
    return value - int(datetime.now(tz=TZ_VIETNAM).timestamp()) > min_seconds


def _headers_from_block(block: M3UBlock) -> dict[str, str]:
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "*/*", "Cache-Control": "no-cache"}
    for line in block.lines:
        stripped = line.strip()
        if stripped.lower().startswith("#extvlcopt:http-referrer="):
            headers["Referer"] = stripped.split("=", 1)[1]
        elif stripped.lower().startswith("#extvlcopt:http-user-agent="):
            headers["User-Agent"] = stripped.split("=", 1)[1]
    raw = clean_text(block.url_line)
    if "|" in raw:
        _url, pipe = raw.split("|", 1)
        for part in pipe.split("&"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = unquote(key).strip()
            value = unquote(value).strip()
            if key and value:
                headers[key] = value
    page_url = clean_text(block.metadata.get("page_url"))
    if page_url and "Referer" not in headers:
        headers["Referer"] = page_url
    return headers


def _probe_previous_block(block: M3UBlock, timeout: int) -> tuple[bool, str]:
    if not _signed_url_not_expired(block.canonical_url):
        return False, "signed-expired"
    headers = _headers_from_block(block)
    headers.setdefault("Range", "bytes=0-4095")
    request = urllib.request.Request(block.canonical_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()) or 0)
            content_type = clean_text(response.headers.get("Content-Type", "")).lower()
            try:
                data = response.read(4096)
            except (TimeoutError, OSError) as exc:
                if status in {200, 206} and block.kind == "flv" and "flv" in content_type:
                    return True, f"HTTP {status}; streaming body ({type(exc).__name__})"
                return False, f"HTTP {status}; read {type(exc).__name__}"
            if block.kind == "flv":
                return status in {200, 206} and data.startswith(b"FLV"), f"HTTP {status}; flv={data.startswith(b'FLV')}"
            if block.kind == "m3u8":
                ok = status in {200, 206} and b"#EXTM3U" in data.upper()
                return ok, f"HTTP {status}; m3u8={ok}"
            return False, "unsupported-kind"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def recover_previous_last_good(root: Path, now: datetime) -> tuple[list[M3UBlock], list[dict[str, Any]]]:
    if not read_env_bool("MULTI_LAST_GOOD_ENABLED", True):
        return [], []
    candidates = _parse_previous_playlist(root, now)
    max_candidates = max(0, min(int(os.getenv("MULTI_LAST_GOOD_MAX_CANDIDATES", "40")), 200))
    timeout = max(2, min(int(os.getenv("MULTI_LAST_GOOD_TIMEOUT_SECONDS", "5")), 20))
    workers = max(1, min(int(os.getenv("MULTI_LAST_GOOD_WORKERS", "6")), 12))
    unknown_ttl = max(1, min(int(os.getenv("MULTI_LAST_GOOD_UNKNOWN_TTL_MINUTES", "90")), 720))
    filtered: list[M3UBlock] = []
    audit: list[dict[str, Any]] = []
    for block in candidates:
        if block.kickoff:
            if not block_within_source_window(block, now, require_known_time=True):
                audit.append({"url": block.canonical_url, "kept": False, "reason": "outside-source-window"})
                continue
        else:
            generated = _parse_datetime_value(block.metadata.get("prior_generated_at"))
            if not generated or (now - generated).total_seconds() > unknown_ttl * 60:
                audit.append({"url": block.canonical_url, "kept": False, "reason": "unknown-time-stale"})
                continue
        filtered.append(block)
        if len(filtered) >= max_candidates:
            break
    recovered: list[M3UBlock] = []
    if not filtered:
        return recovered, audit
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="last-good") as executor:
        futures = {executor.submit(_probe_previous_block, block, timeout): block for block in filtered}
        for future in as_completed(futures):
            block = futures[future]
            try:
                ok, reason = future.result()
            except Exception as exc:
                ok, reason = False, f"{type(exc).__name__}: {exc}"
            audit.append({"url": block.canonical_url, "kept": ok, "reason": reason, "source": block.source_key})
            if not ok:
                continue
            block.metadata["recovered_last_good"] = True
            block.metadata["last_good_probe"] = reason
            block.metadata["playability"] = "verified"
            block.metadata["http_status"] = 200
            block.playability = "verified"
            recovered.append(block)
    return recovered, audit


def _write_variant(path: Path, selected: list[M3UBlock], maps: dict[str, dict[str, M3UBlock]], fallback_maps: dict[str, dict[str, M3UBlock]]) -> None:
    lines = ["#EXTM3U"]
    for item in selected:
        block = maps.get(item.source_key, {}).get(item.canonical_url) or fallback_maps.get(item.source_key, {}).get(item.canonical_url) or item
        lines.extend(_render_source_grouped_lines(block))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cleanup_intermediate_playlists(root: Path) -> list[str]:
    """Chỉ giữ playlist tổng; xóa cả file tạm và layout thư mục sai của v4.4.1."""
    keep = (root / "all_live.m3u").resolve()
    removed: list[str] = []
    for path in root.rglob("*.m3u"):
        if path.resolve() == keep:
            continue
        try:
            removed.append(path.relative_to(root).as_posix())
            path.unlink()
        except FileNotFoundError:
            pass
    for folder_name in ("chuoichien", "luongson", "gavang", "xoilac", "colatv", "phaohoa"):
        folder = root / folder_name
        try:
            folder.rmdir()
        except OSError:
            pass
    return sorted(removed)


def merge_sources(
    root: Path,
    sources: list[SourceFiles],
    *,
    now: datetime | None = None,
    max_per_match: int | None = None,
    upcoming_hours: int | None = None,
    preserve_on_empty: bool = True,
) -> dict[str, Any]:
    now = (now or datetime.now(TZ_VIETNAM)).astimezone(TZ_VIETNAM)
    max_per_match = max_per_match or max(1, min(int(os.getenv("MULTI_MAX_STREAMS_PER_MATCH", "2")), 6))
    upcoming_hours = upcoming_hours or max(1, min(int(os.getenv("MULTI_UPCOMING_KEEP_HOURS", "4")), 24))

    previous_last_good, last_good_audit = recover_previous_last_good(root, now)
    all_blocks: list[M3UBlock] = []
    metadata_references: list[M3UBlock] = []
    universal_maps: dict[str, dict[str, M3UBlock]] = {}
    source_stats: list[dict[str, Any]] = []

    for source in sources:
        blocks = parse_m3u(source.universal, source.key, source.label)
        blocks, debug_rows = enrich_blocks(source, blocks, now)
        source_references = load_debug_metadata_references(source, now)
        metadata_references.extend(source_references)
        # v4.4.26: card vẫn được lưu đầy đủ trong debug/state nhưng all_live.m3u chỉ nhận stream phát được.
        catalog_placeholders: list[M3UBlock] = []
        if not verified_only_enabled() and source.key != "phaohoa":
            for ref_index, ref in enumerate(source_references):
                if not bool(ref.metadata.get("listed_in_playlist") or ref.metadata.get("catalog_only")):
                    continue
                placeholder = build_catalog_placeholder(source, ref.metadata, ref_index, now)
                if placeholder:
                    catalog_placeholders.append(placeholder)
        combined_blocks = list(blocks) + catalog_placeholders
        universal_maps[source.key] = {item.canonical_url: item for item in combined_blocks}
        if source.returncode == 0 and debug_rows > 0:
            all_blocks.extend(combined_blocks)
        source_stats.append({
            "key": source.key,
            "label": source.label,
            "returncode": source.returncode,
            "fresh": source.fresh,
            "debug_rows": debug_rows,
            "playlist_blocks": len(blocks),
            "catalog_placeholders": len(catalog_placeholders) if source.key != "phaohoa" else 0,
            "metadata_references": len(source_references),
            "included": source.returncode == 0 and debug_rows > 0,
        })

    all_blocks.extend(previous_last_good)
    gavang_metadata_stats = enrich_gavang_metadata_from_other_sources(all_blocks, metadata_references)
    gavang_logo_stats = enrich_gavang_logos_from_other_sources(all_blocks, metadata_references)
    selected, dropped = choose_candidates(all_blocks, now, max_per_match, upcoming_hours)
    outputs = {
        "playlist": root / "all_live.m3u",
        "debug": root / "all_live_debug.json",
    }

    if selected:
        _write_variant(outputs["playlist"], selected, universal_maps, universal_maps)
    elif not preserve_on_empty:
        outputs["playlist"].write_text("#EXTM3U\n", encoding="utf-8")

    channels = []
    for item in selected:
        channels.append({
            "source": item.source_key,
            "source_label": item.source_label,
            "url": item.canonical_url,
            "match_key": item.match_key,
            "display_name": item.display_name,
            "group": item.source_label,
            "sport_group": item.attributes.get("group-title", "Khác"),
            "quality": item.quality,
            "kind": item.kind,
            "playability": item.playability,
            "classification": item.metadata.get("classification"),
            "classification_reason": item.metadata.get("classification_reason"),
            "has_secret": bool(item.metadata.get("has_secret")),
            "expiry": item.metadata.get("expiry"),
            "derived_pending": bool(item.metadata.get("derived_pending")),
            "pending_reason": item.metadata.get("pending_reason"),
            "score": item.score,
            "kickoff_iso": item.kickoff.isoformat() if item.kickoff else None,
            "minutes_to_kickoff": round((item.kickoff - now).total_seconds() / 60, 2) if item.kickoff else item.metadata.get("minutes_to_kickoff"),
            "metadata_audit": item.metadata.get("metadata_audit"),
            "metadata_enriched_from": item.metadata.get("metadata_enriched_from"),
            "metadata_warning": item.metadata.get("metadata_warning"),
            "stream_key_tokens": item.metadata.get("stream_key_tokens"),
            "logo": item.attributes.get("tvg-logo"),
            "logo_source": item.metadata.get("logo_source"),
            "logo_enriched_from": item.metadata.get("logo_enriched_from"),
            "logo_is_fallback": item.metadata.get("logo_is_fallback"),
            "entry_mode": "metadata-only" if is_metadata_placeholder(item) else "stream",
            "page_url": item.attributes.get("catalog-page-url") or item.attributes.get("phaohoa-page-url") or item.metadata.get("url"),
            "home_logo": item.attributes.get("phaohoa-home-logo") or item.metadata.get("home_logo"),
            "away_logo": item.attributes.get("phaohoa-away-logo") or item.metadata.get("away_logo"),
            "blv": item.attributes.get("phaohoa-blv") or item.metadata.get("blv"),
            "recovered_last_good": bool(item.metadata.get("recovered_last_good")),
            "last_good_probe": item.metadata.get("last_good_probe"),
            "prior_generated_at": item.metadata.get("prior_generated_at"),
        })

    report = {
        "version": VERSION,
        "generated_at": now.isoformat(),
        "policy": {
            "max_streams_per_match_blv": max_per_match,
            "upcoming_keep_hours": upcoming_hours,
            "verified_only": verified_only_enabled(),
            "requires_verified_or_observed": True,
            "allows_pending": not verified_only_enabled(),
            "allows_metadata_only": not verified_only_enabled(),
            "last_good_enabled": read_env_bool("MULTI_LAST_GOOD_ENABLED", True),
            "pending_past_minutes": max(0, min(int(os.getenv("MULTI_PENDING_PAST_MINUTES", "150")), 1440)),
            "keep_gavang_unknown_pending": os.getenv("MULTI_KEEP_GAVANG_UNKNOWN_PENDING", "1").strip().lower() not in {"0", "false", "no", "off"},
        },
        "sources": source_stats,
        "input_candidates": len(all_blocks),
        "metadata_reference_count": len(metadata_references),
        "last_good_recovered_count": len(previous_last_good),
        "last_good_audit": last_good_audit,
        "selected_count": len(selected),
        "dropped_count": len(dropped),
        "gavang_metadata": gavang_metadata_stats,
        "gavang_logo": gavang_logo_stats,
        "channels": channels,
        "dropped": dropped,
        "outputs_written": bool(selected),
    }
    outputs["debug"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
