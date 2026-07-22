from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

from playwright.async_api import Browser, BrowserContext, Page, Request, Response, async_playwright

VERSION = "4.4.11-XOILAC-MULTISOURCE-ADAPTER"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_START_URL = "https://xoilacz.io/"
DEFAULT_HOME_URLS = ("https://xoilacz.io/", "https://malaysiandigest.com/", "https://altenergystocks.com/")
VN_TZ = ZoneInfo("Asia/Ho_Chi_Minh")

OUTPUT_M3U = ROOT / "xoilac_live.m3u"
OUTPUT_PIPE_M3U = ROOT / "xoilac_live_pipe.m3u"
OUTPUT_VLC_M3U = ROOT / "xoilac_live_vlc.m3u"
OUTPUT_VERIFIED_M3U = ROOT / "xoilac_runner_verified.m3u"
OUTPUT_ALL_M3U = ROOT / "xoilac_all_candidates.m3u"
OUTPUT_REJECTED_M3U = ROOT / "xoilac_rejected.m3u"
OUTPUT_DEBUG = ROOT / "xoilac_debug.json"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)

MEDIA_URL_RE = re.compile(r"\.(?:m3u8|flv|mpd)(?:[?#]|$)", re.I)
MATCH_URL_RE = re.compile(r"/truc-tiep/", re.I)
SOURCE_LINK_RE = re.compile(r"/truc-tiep/.+?/link/(\d+)/?$", re.I)
PLAYER_URL_RE = re.compile(
    r"(?:livepingscorex\.com|apisportpulse\.com|streambylivepulse\.com|"
    r"/ajax/chanel/|/ajax/channel/)",
    re.I,
)
PLAYER_TYPE_RE = re.compile(r"/type/(\d+)/link/([^/?#]+)", re.I)
CONTENT_TYPE_RE = re.compile(
    r"(?:video/x-flv|application/vnd\.apple\.mpegurl|application/x-mpegurl|"
    r"application/dash\+xml|video/mp2t)",
    re.I,
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
    "video",
)
GENERIC_SOURCE_LABELS = {
    "",
    "trực tiếp",
    "xem",
    "xem ngay",
    "link",
    "server",
    "kênh",
    "live",
}


def read_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def read_env_urls(name: str, defaults: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name, "").strip()
    values = [part.strip() for part in raw.split(",") if part.strip()] if raw else list(defaults)
    result: list[str] = []
    for value in values:
        if not value.startswith(("http://", "https://")):
            continue
        normalized = value.rstrip("/") + "/"
        if normalized not in result:
            result.append(normalized)
    return tuple(result or defaults)


HOME_URLS = read_env_urls("XOILAC_HOME_URLS", DEFAULT_HOME_URLS)


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_header_map(headers: dict[str, str] | None) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in (headers or {}).items()}


def canonical_match_url(url: str) -> str:
    """Bỏ /link/N, query và fragment để lấy URL gốc của trận."""
    parsed = urlparse(clean_text(url))
    path = re.sub(r"/link/\d+/?$", "/", parsed.path, flags=re.I)
    if not path.endswith("/"):
        path += "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def source_index_from_url(url: str) -> int:
    match = re.search(r"/link/(\d+)/?$", urlparse(url).path, re.I)
    return int(match.group(1)) if match else 0


def clean_commentator_label(value: str, source_index: int = 0) -> str:
    text = clean_text(value)
    text = re.sub(r"^[▶►•\-–—|]+\s*", "", text)
    text = re.sub(r"^(?:BLV|Bình\s*luận\s*viên)\s*[:\-–—]?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*[▶►•|]+$", "", text)
    if text.lower() in GENERIC_SOURCE_LABELS:
        return f"Kênh {source_index + 1}"
    return text or f"Kênh {source_index + 1}"


def is_media_candidate(url: str, content_type: str = "") -> bool:
    """Nhận đúng media; không nhầm flv.min.js thành luồng FLV."""
    return bool(MEDIA_URL_RE.search(url or "") or CONTENT_TYPE_RE.search(content_type or ""))


def media_kind(url: str, content_type: str = "") -> str:
    lowered_type = (content_type or "").lower()
    extension_match = re.search(r"\.(m3u8|flv|mpd)(?:[?#]|$)", url or "", re.I)
    if extension_match:
        return extension_match.group(1).lower()
    if "video/x-flv" in lowered_type:
        return "flv"
    if "mpegurl" in lowered_type:
        return "m3u8"
    if "dash+xml" in lowered_type:
        return "mpd"
    return "media"


def parse_signed_expiry(url: str) -> dict[str, Any]:
    query = parse_qs(urlparse(url).query)
    raw = (query.get("wsABSTime") or query.get("expires") or query.get("expire") or [""])[0]
    try:
        timestamp = int(raw, 16 if re.search(r"[a-f]", str(raw), re.I) else 10)
    except (TypeError, ValueError):
        return {"timestamp": None, "utc": "", "vietnam": "", "seconds_left": None}

    now = int(time.time())
    utc_dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    vn_dt = utc_dt.astimezone(VN_TZ)
    return {
        "timestamp": timestamp,
        "utc": utc_dt.isoformat(),
        "vietnam": vn_dt.isoformat(),
        "seconds_left": timestamp - now,
    }


def has_stream_secret(url: str) -> bool:
    query = parse_qs(urlparse(url).query)
    return bool((query.get("wsSecret") or [""])[0])


def derive_match_metadata(url: str, fallback_title: str = "") -> dict[str, str]:
    parsed = urlparse(canonical_match_url(url))
    slug = unquote(parsed.path.rstrip("/").split("/")[-1])
    date_value = ""
    time_value = ""

    match = re.search(
        r"^(.*?)-luc-(\d{2})(\d{2})-ngay-(\d{2})-(\d{2})-(\d{4})$",
        slug,
        re.I,
    )
    if match:
        raw_name, hour, minute, day, month, year = match.groups()
        name = re.sub(r"[-_]+", " ", raw_name)
        name = re.sub(r"\bvs\b", " vs ", name, flags=re.I)
        name = clean_text(name).title().replace(" Vs ", " vs ")
        time_value = f"{hour}:{minute}"
        date_value = f"{day}/{month}/{year}"
    else:
        name = clean_text(fallback_title)
        name = re.sub(r"\s*[-–|]\s*Xoilac.*$", "", name, flags=re.I)
        name = re.sub(r"^Trực tiếp\s+", "", name, flags=re.I)
        name = re.sub(r"\s+vào lúc.*$", "", name, flags=re.I)
        if not name:
            name = clean_text(re.sub(r"[-_]+", " ", slug)).title() or "Xoilac stream"

    home_name = ""
    away_name = ""
    if re.search(r"\s+vs\s+", name, re.I):
        home_name, away_name = [clean_text(part) for part in re.split(r"\s+vs\s+", name, maxsplit=1, flags=re.I)]
    return {
        "name": name,
        "time": time_value,
        "date": date_value,
        "home_name": home_name,
        "away_name": away_name,
    }


def parse_match_datetime(url: str) -> datetime | None:
    metadata = derive_match_metadata(url)
    if not metadata["date"] or not metadata["time"]:
        return None
    try:
        return datetime.strptime(
            f"{metadata['date']} {metadata['time']}", "%d/%m/%Y %H:%M"
        ).replace(tzinfo=VN_TZ)
    except ValueError:
        return None


def stable_channel_id(url: str, index: int, source_index: int = 0) -> str:
    parsed = urlparse(canonical_match_url(url))
    path = re.sub(r"[^a-zA-Z0-9]+", "-", parsed.path).strip("-")
    return f"xoilac-{path[-64:] or index}-s{source_index}-{index}"


def parse_player_identity(urls: Iterable[str]) -> tuple[str, str]:
    for url in urls:
        match = PLAYER_TYPE_RE.search(url or "")
        if match:
            return match.group(1), match.group(2)
    return "", ""


def classify_stream(entry: "StreamCapture") -> None:
    parsed = urlparse(entry.url)
    hostname = parsed.hostname or ""
    entry.has_secret = has_stream_secret(entry.url)
    entry.expiry = parse_signed_expiry(entry.url)
    seconds_left = entry.expiry.get("seconds_left")
    expiry_ok = seconds_left is None or seconds_left > 60
    status = entry.status
    status_ok = status in {200, 206}

    entry.placeholder_suspected = bool(
        not entry.has_secret
        and (
            entry.player_type == "8"
            or hostname.lower().startswith("live2.")
            or "live2.streambylivepulse.com" in hostname.lower()
        )
    )

    if entry.placeholder_suspected:
        entry.classification = "placeholder_or_ad"
        entry.publishable = False
        entry.classification_reason = (
            "Player type/8 hoặc live2 không có wsSecret; mẫu này trong log thực tế phát banner/quảng cáo."
        )
        return

    if status in {404, 410}:
        entry.classification = "dead"
        entry.publishable = False
        entry.classification_reason = f"HTTP {status}"
        return

    if entry.has_secret and expiry_ok:
        if status == 403:
            entry.classification = "signed_runner_blocked"
            entry.publishable = True
            entry.classification_reason = (
                "URL có wsSecret còn hạn nhưng GitHub runner bị HTTP 403; vẫn giữ trong M3U chính "
                "để client mạng ngoài GitHub thử phát."
            )
        elif status_ok or status is None:
            entry.classification = "signed"
            entry.publishable = True
            entry.classification_reason = "URL có wsSecret còn hạn."
        else:
            entry.classification = "signed_http_error"
            entry.publishable = False
            entry.classification_reason = f"URL có token nhưng HTTP {status}."
        return

    if entry.has_secret and not expiry_ok:
        entry.classification = "expired"
        entry.publishable = False
        entry.classification_reason = "wsSecret đã hết hạn hoặc sắp hết hạn."
        return

    if status_ok and (entry.probe_ok or entry.verified):
        entry.classification = "verified_unsigned"
        entry.publishable = True
        entry.classification_reason = "Luồng không token nhưng HTTP 200 và đúng định dạng media."
        return

    entry.classification = "unverified"
    entry.publishable = False
    entry.classification_reason = entry.verify_reason or f"HTTP {status} chưa xác minh."


@dataclass
class StreamCapture:
    url: str
    kind: str = "media"
    referer: str = ""
    origin: str = ""
    user_agent: str = UA
    frame_url: str = ""
    page_url: str = ""
    source_url: str = ""
    commentator: str = ""
    source_index: int = 0
    player_type: str = ""
    player_channel: str = ""
    status: int | None = None
    statuses: list[int] = field(default_factory=list)
    content_type: str = ""
    sources: list[str] = field(default_factory=list)
    first_seen_at: str = ""
    verified: bool = False
    probe_ok: bool = False
    verify_reason: str = ""
    expiry: dict[str, Any] = field(default_factory=dict)
    has_secret: bool = False
    placeholder_suspected: bool = False
    publishable: bool = False
    classification: str = ""
    classification_reason: str = ""

    def merge(
        self,
        *,
        source: str,
        headers: dict[str, str] | None = None,
        frame_url: str = "",
        page_url: str = "",
        status: int | None = None,
        content_type: str = "",
    ) -> None:
        normalized = normalize_header_map(headers)
        if source and source not in self.sources:
            self.sources.append(source)
        if normalized.get("referer"):
            self.referer = normalized["referer"]
        if normalized.get("origin"):
            self.origin = normalized["origin"]
        if normalized.get("user-agent"):
            self.user_agent = normalized["user-agent"]
        if frame_url:
            self.frame_url = frame_url
        if page_url:
            self.page_url = page_url
        if status is not None:
            self.status = int(status)
            if int(status) not in self.statuses:
                self.statuses.append(int(status))
        if content_type:
            self.content_type = content_type
            self.kind = media_kind(self.url, content_type)
        if not self.first_seen_at:
            self.first_seen_at = datetime.now(VN_TZ).isoformat()
        self.expiry = parse_signed_expiry(self.url)
        if self.status == 200 and is_media_candidate(self.url, self.content_type):
            self.verified = True
            self.verify_reason = f"browser HTTP {self.status}; content-type={self.content_type or 'unknown'}"
        classify_stream(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "referer": self.referer,
            "origin": self.origin,
            "user_agent": self.user_agent,
            "frame_url": self.frame_url,
            "page_url": self.page_url,
            "source_url": self.source_url,
            "commentator": self.commentator,
            "source_index": self.source_index,
            "player_type": self.player_type,
            "player_channel": self.player_channel,
            "status": self.status,
            "statuses": self.statuses,
            "content_type": self.content_type,
            "sources": self.sources,
            "first_seen_at": self.first_seen_at,
            "verified": self.verified,
            "probe_ok": self.probe_ok,
            "verify_reason": self.verify_reason,
            "expiry": self.expiry,
            "has_secret": self.has_secret,
            "placeholder_suspected": self.placeholder_suspected,
            "publishable": self.publishable,
            "classification": self.classification,
            "classification_reason": self.classification_reason,
        }


class CaptureCollector:
    def __init__(self, *, source_url: str = "", commentator: str = "", source_index: int = 0) -> None:
        self.streams: dict[str, StreamCapture] = {}
        self.player_urls: list[str] = []
        self.request_tasks: set[asyncio.Task[Any]] = set()
        self.response_tasks: set[asyncio.Task[Any]] = set()
        self.first_media_event = asyncio.Event()
        self.source_url = source_url
        self.commentator = commentator
        self.source_index = source_index

    def remember_player_url(self, url: str) -> None:
        if url and PLAYER_URL_RE.search(url) and url not in self.player_urls:
            self.player_urls.append(url)
            print(f"      🧩 Player/iframe: {url}", flush=True)
            player_type, player_channel = parse_player_identity([url])
            if player_type or player_channel:
                for entry in self.streams.values():
                    entry.player_type = entry.player_type or player_type
                    entry.player_channel = entry.player_channel or player_channel
                    classify_stream(entry)

    def get_or_create(self, url: str, content_type: str = "") -> StreamCapture:
        entry = self.streams.get(url)
        if entry is None:
            player_type, player_channel = parse_player_identity(self.player_urls)
            entry = StreamCapture(
                url=url,
                kind=media_kind(url, content_type),
                source_url=self.source_url,
                commentator=self.commentator,
                source_index=self.source_index,
                player_type=player_type,
                player_channel=player_channel,
            )
            self.streams[url] = entry
            print(f"      🎯 Bắt media: {url}", flush=True)
        self.first_media_event.set()
        return entry

    async def handle_request_async(self, request: Request) -> None:
        try:
            url = request.url
            self.remember_player_url(url)
            if not is_media_candidate(url):
                return
            try:
                headers = await request.all_headers()
            except Exception:
                headers = request.headers
            frame_url = ""
            page_url = ""
            try:
                frame_url = request.frame.url
                page_url = request.frame.page.url
            except Exception:
                pass
            self.get_or_create(url).merge(
                source="request",
                headers=headers,
                frame_url=frame_url,
                page_url=page_url,
            )
        except Exception as exc:
            print(f"      ⚠️ Lỗi xử lý request: {type(exc).__name__}: {exc}", flush=True)

    async def handle_response_async(self, response: Response) -> None:
        try:
            url = response.url
            content_type = response.headers.get("content-type", "")
            self.remember_player_url(url)
            if not is_media_candidate(url, content_type):
                return
            request = response.request
            try:
                headers = await request.all_headers()
            except Exception:
                headers = request.headers
            frame_url = ""
            page_url = ""
            try:
                frame_url = request.frame.url
                page_url = request.frame.page.url
            except Exception:
                pass
            entry = self.get_or_create(url, content_type)
            entry.merge(
                source="response",
                headers=headers,
                frame_url=frame_url,
                page_url=page_url,
                status=response.status,
                content_type=content_type,
            )
            icon = "✅" if response.status == 200 else "⚠️" if response.status == 403 else "❌"
            print(
                f"      {icon} Media response: HTTP {response.status} | "
                f"{content_type or 'không có content-type'}",
                flush=True,
            )
        except Exception as exc:
            print(f"      ⚠️ Lỗi xử lý response: {type(exc).__name__}: {exc}", flush=True)

    def on_request(self, request: Request) -> None:
        task = asyncio.create_task(self.handle_request_async(request))
        self.request_tasks.add(task)
        task.add_done_callback(self.request_tasks.discard)

    def on_response(self, response: Response) -> None:
        task = asyncio.create_task(self.handle_response_async(response))
        self.response_tasks.add(task)
        task.add_done_callback(self.response_tasks.discard)

    async def flush(self) -> None:
        pending = list(self.request_tasks | self.response_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def probe_stream_sync(entry: StreamCapture, timeout: int = 10) -> tuple[bool, str]:
    headers = {
        "User-Agent": entry.user_agent or UA,
        "Accept": "*/*",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if entry.referer:
        headers["Referer"] = entry.referer
    if entry.origin:
        headers["Origin"] = entry.origin

    request = urllib.request.Request(entry.url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", response.getcode()) or 0)
            content_type = response.headers.get("Content-Type", "")
            reader = getattr(response, "read1", None)
            try:
                data = reader(64) if callable(reader) else response.read(64)
            except (TimeoutError, OSError) as exc:
                kind = media_kind(entry.url, content_type)
                if status in {200, 206} and (
                    (kind == "flv" and "flv" in content_type.lower())
                    or (kind == "m3u8" and "mpegurl" in content_type.lower())
                    or (kind == "mpd" and "dash+xml" in content_type.lower())
                ):
                    return True, f"HTTP {status}; content-type={content_type}; body streaming ({type(exc).__name__})"
                return False, f"HTTP {status}; đọc body lỗi {type(exc).__name__}: {exc}"
            kind = media_kind(entry.url, content_type)
            if kind == "flv":
                ok = status == 200 and data.startswith(b"FLV")
                return ok, f"HTTP {status}; FLV header={'đúng' if data.startswith(b'FLV') else 'sai'}"
            if kind == "m3u8":
                ok = status == 200 and b"#EXTM3U" in data.upper()
                return ok, f"HTTP {status}; M3U8 header={'đúng' if ok else 'chưa thấy'}"
            if kind == "mpd":
                ok = status == 200 and b"<MPD" in data.upper()
                return ok, f"HTTP {status}; MPD header={'đúng' if ok else 'chưa thấy'}"
            return status == 200, f"HTTP {status}; content-type={content_type}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTPError {exc.code}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


async def verify_streams(entries: Iterable[StreamCapture], timeout: int) -> None:
    for entry in entries:
        ok, reason = await asyncio.to_thread(probe_stream_sync, entry, timeout)
        entry.probe_ok = ok
        entry.verified = entry.verified or ok
        entry.verify_reason = reason
        classify_stream(entry)
        print(f"      {'✅' if ok else '❌'} Probe {entry.kind}: {reason}", flush=True)


async def click_play_controls(page: Page) -> int:
    clicked = 0
    for frame in page.frames:
        try:
            await frame.evaluate(
                """
                () => {
                  for (const video of document.querySelectorAll('video')) {
                    video.muted = true;
                    const promise = video.play();
                    if (promise && promise.catch) promise.catch(() => {});
                  }
                }
                """
            )
        except Exception:
            pass

        for selector in PLAY_SELECTORS:
            try:
                locator = frame.locator(selector)
                count = min(await locator.count(), 3)
                for index in range(count):
                    item = locator.nth(index)
                    if selector == "video":
                        continue
                    try:
                        if await item.is_visible(timeout=300):
                            await item.click(timeout=800, force=True)
                            clicked += 1
                            print(f"      ▶️ Đã bấm play trong frame: {frame.url[:140]}", flush=True)
                            break
                    except Exception:
                        continue
            except Exception:
                continue
    return clicked


async def extract_page_title(page: Page) -> str:
    selectors = ("h1", ".match-title", ".title", "title")
    for selector in selectors:
        try:
            value = await page.title() if selector == "title" else await page.locator(selector).first.text_content(timeout=800)
            value = clean_text(value)
            if value:
                return value
        except Exception:
            continue
    return ""


async def extract_match_page_data(page: Page, match_url: str, title: str = "") -> dict[str, Any]:
    base = derive_match_metadata(match_url, title)
    try:
        payload = await page.evaluate(
            r"""
            () => {
              const abs = value => {
                try { return new URL(value, location.href).href; } catch (_) { return ''; }
              };
              const text = node => (node?.innerText || node?.textContent || '').replace(/\s+/g, ' ').trim();
              const links = Array.from(document.querySelectorAll('a[href]')).map(a => ({
                url: abs(a.getAttribute('href')),
                text: text(a),
                title: (a.getAttribute('title') || '').trim(),
                aria: (a.getAttribute('aria-label') || '').trim()
              }));
              const images = Array.from(document.querySelectorAll('img')).map(img => ({
                url: abs(img.currentSrc || img.src || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || ''),
                alt: (img.alt || '').trim(),
                title: (img.title || '').trim()
              })).filter(row => row.url);
              const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5')).map(text).filter(Boolean);
              return {
                links,
                images,
                headings,
                body_text: (document.body?.innerText || '').slice(0, 50000)
              };
            }
            """
        )
    except Exception:
        payload = {"links": [], "images": [], "headings": [], "body_text": ""}

    canonical = canonical_match_url(match_url)
    source_map: dict[str, dict[str, Any]] = {}
    for row in payload.get("links", []):
        source_url = clean_text(row.get("url"))
        if not SOURCE_LINK_RE.search(urlparse(source_url).path):
            continue
        if canonical_match_url(source_url) != canonical:
            continue
        source_index = source_index_from_url(source_url)
        raw_label = clean_text(row.get("text") or row.get("title") or row.get("aria"))
        commentator = clean_commentator_label(raw_label, source_index)
        existing = source_map.get(source_url)
        if not existing or len(commentator) > len(existing["commentator"]):
            source_map[source_url] = {
                "url": source_url,
                "index": source_index,
                "commentator": commentator,
            }

    source_links = sorted(source_map.values(), key=lambda row: (row["index"], row["url"]))
    if not source_links:
        source_links = [{"url": canonical, "index": 0, "commentator": "Xoilac"}]

    team_images: list[dict[str, str]] = []
    for image in payload.get("images", []):
        image_url = clean_text(image.get("url"))
        if not image_url:
            continue
        if re.search(r"/(?:football|basketball|volleyball|tennis|esports)/team/", image_url, re.I) or re.search(r"/team/[^/]+/image/", image_url, re.I):
            if image_url not in [item["url"] for item in team_images]:
                team_images.append({
                    "url": image_url,
                    "alt": clean_text(image.get("alt")),
                    "title": clean_text(image.get("title")),
                })

    home_logo = team_images[0]["url"] if team_images else ""
    away_logo = team_images[1]["url"] if len(team_images) > 1 else ""
    body_text = clean_text(payload.get("body_text"))
    league = ""
    for heading in payload.get("headings", []):
        match = re.search(r"Tường thuật miễn phí trận đấu.+?\s+-\s+(.+)$", clean_text(heading), re.I)
        if match:
            league = clean_text(match.group(1))
            break
    if not league:
        match = re.search(r"Tường thuật miễn phí trận đấu.+?\s+-\s+([^\n]{2,100})", body_text, re.I)
        if match:
            league = clean_text(match.group(1))

    return {
        **base,
        "canonical_url": canonical,
        "home_logo": home_logo,
        "away_logo": away_logo,
        "logo_candidates": team_images,
        "league": league,
        "source_links": source_links,
    }


async def collect_match_links(page: Page) -> list[str]:
    try:
        values = await page.locator("a[href*='/truc-tiep/']").evaluate_all(
            "nodes => nodes.map(node => node.href).filter(Boolean)"
        )
    except Exception:
        values = []
    unique: list[str] = []
    for value in values:
        value = clean_text(value)
        if not value or not MATCH_URL_RE.search(urlparse(value).path):
            continue
        canonical = canonical_match_url(value)
        if canonical not in unique:
            unique.append(canonical)
    return unique


def filter_scan_window(
    urls: list[str],
    *,
    past_minutes: int,
    future_minutes: int,
    max_matches: int,
) -> list[str]:
    now = datetime.now(VN_TZ)
    timed: list[tuple[float, str]] = []
    unknown: list[str] = []
    for url in urls:
        match_dt = parse_match_datetime(url)
        if match_dt is None:
            unknown.append(url)
            continue
        delta_minutes = (match_dt - now).total_seconds() / 60
        if -past_minutes <= delta_minutes <= future_minutes:
            timed.append((abs(delta_minutes), url))
    timed.sort(key=lambda item: item[0])
    selected = [url for _, url in timed]
    if len(selected) < max_matches:
        selected.extend(url for url in unknown if url not in selected)
    return selected[:max_matches]


async def discover_targets(
    context: BrowserContext,
    start_url: str,
    args: argparse.Namespace,
) -> tuple[list[str], dict[str, Any]]:
    page = await context.new_page()
    discovery: dict[str, Any] = {
        "input_url": start_url,
        "final_url": "",
        "redirect_chain": [],
        "all_match_links": [],
    }

    def track_navigation(request: Request) -> None:
        if request.is_navigation_request():
            discovery["redirect_chain"].append(request.url)

    page.on("request", track_navigation)
    try:
        print(f"🌐 Mở nguồn: {start_url}", flush=True)
        await page.goto(start_url, wait_until="domcontentloaded", timeout=args.navigation_timeout * 1000)
        await page.wait_for_timeout(args.home_wait * 1000)
        discovery["final_url"] = page.url
        print(f"↪️ URL cuối sau chuyển hướng: {page.url}", flush=True)

        if MATCH_URL_RE.search(page.url):
            return [canonical_match_url(page.url)], discovery

        links = await collect_match_links(page)
        discovery["all_match_links"] = links
        selected = filter_scan_window(
            links,
            past_minutes=args.past_minutes,
            future_minutes=args.future_minutes,
            max_matches=args.max_matches,
        )
        print(
            f"✅ Trang chủ có {len(links)} trận duy nhất; chọn {len(selected)} trận gần giờ để quét.",
            flush=True,
        )
        return selected, discovery
    finally:
        await page.close()


def add_cache_buster(url: str, attempt: int) -> str:
    if attempt <= 1:
        return url
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["_xoilac_refresh"] = [str(int(time.time() * 1000))]
    flat = [(key, value) for key, values in query.items() for value in values]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(flat), parsed.fragment))


async def capture_source(
    context: BrowserContext,
    source: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, Any]:
    source_url = clean_text(source.get("url"))
    source_index = int(source.get("index", 0))
    commentator = clean_commentator_label(clean_text(source.get("commentator")), source_index)
    errors: list[str] = []
    all_entries: dict[str, StreamCapture] = {}
    all_player_urls: list[str] = []
    started = time.monotonic()

    for attempt in range(1, args.token_refresh_attempts + 1):
        collector = CaptureCollector(
            source_url=source_url,
            commentator=commentator,
            source_index=source_index,
        )
        context.on("request", collector.on_request)
        context.on("response", collector.on_response)
        page = await context.new_page()
        direct_player_pages: list[Page] = []
        attempt_url = add_cache_buster(source_url, attempt)
        print(
            f"   🎙️ Nguồn {source_index + 1}: {commentator} | lần {attempt}/{args.token_refresh_attempts}",
            flush=True,
        )
        try:
            await page.goto(
                attempt_url,
                wait_until="domcontentloaded",
                timeout=args.navigation_timeout * 1000,
            )
            await page.wait_for_timeout(1000)
            for delay in (0, 2):
                if delay:
                    await page.wait_for_timeout(delay * 1000)
                await click_play_controls(page)
                if collector.first_media_event.is_set():
                    break

            if not collector.first_media_event.is_set():
                try:
                    await asyncio.wait_for(
                        collector.first_media_event.wait(),
                        timeout=args.source_wait_seconds,
                    )
                except asyncio.TimeoutError:
                    pass

            await collector.flush()
            frame_urls = [frame.url for frame in page.frames if frame.url and frame.url != "about:blank"]
            for frame_url in frame_urls:
                collector.remember_player_url(frame_url)

            if not collector.streams and collector.player_urls:
                for player_url in collector.player_urls[:3]:
                    if not re.search(r"/ajax/(?:chanel|channel)/", player_url, re.I):
                        continue
                    print(f"      🔁 Fallback mở trực tiếp player: {player_url}", flush=True)
                    player_page = await context.new_page()
                    direct_player_pages.append(player_page)
                    try:
                        await player_page.goto(
                            player_url,
                            wait_until="domcontentloaded",
                            timeout=args.navigation_timeout * 1000,
                            referer=source_url,
                        )
                        await player_page.wait_for_timeout(1000)
                        await click_play_controls(player_page)
                        try:
                            await asyncio.wait_for(
                                collector.first_media_event.wait(),
                                timeout=min(10, args.source_wait_seconds),
                            )
                        except asyncio.TimeoutError:
                            pass
                    except Exception as exc:
                        errors.append(f"player fallback {type(exc).__name__}: {exc}")
                    if collector.streams:
                        break

            await page.wait_for_timeout(args.after_first_wait * 1000 if collector.streams else 300)
            await collector.flush()
            player_type, player_channel = parse_player_identity(collector.player_urls)
            for entry in collector.streams.values():
                entry.player_type = entry.player_type or player_type
                entry.player_channel = entry.player_channel or player_channel
                classify_stream(entry)
            if args.verify and collector.streams:
                await verify_streams(collector.streams.values(), args.verify_timeout)

            for player_url in collector.player_urls:
                if player_url not in all_player_urls:
                    all_player_urls.append(player_url)
            for url, entry in collector.streams.items():
                previous = all_entries.get(url)
                if previous is None or (entry.status == 200 and previous.status != 200):
                    all_entries[url] = entry

            signed_fresh = [
                entry
                for entry in all_entries.values()
                if entry.has_secret
                and (entry.expiry.get("seconds_left") is None or entry.expiry.get("seconds_left", 0) > args.min_token_seconds)
            ]
            type7 = any(parse_player_identity([player_url])[0] == "7" for player_url in all_player_urls)
            if signed_fresh or (all_entries and not type7):
                break
            if attempt < args.token_refresh_attempts:
                print("      🔄 Chưa có token type/7 còn hạn; tải lại nguồn để xin wsSecret mới.", flush=True)
        except Exception as exc:
            errors.append(f"source scan {type(exc).__name__}: {exc}")
        finally:
            try:
                context.remove_listener("request", collector.on_request)
                context.remove_listener("response", collector.on_response)
            except Exception:
                pass
            await collector.flush()
            for player_page in direct_player_pages:
                try:
                    await player_page.close()
                except Exception:
                    pass
            try:
                await page.close()
            except Exception:
                pass

    entries = list(all_entries.values())
    for entry in entries:
        classify_stream(entry)
    return {
        "source_url": source_url,
        "source_index": source_index,
        "commentator": commentator,
        "player_urls": all_player_urls,
        "streams": [entry.as_dict() for entry in entries],
        "errors": errors,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def annotate_multisource_playability(stream: dict[str, Any]) -> None:
    """Ánh xạ phân loại riêng của Xôi Lạc sang schema chung của merger."""
    classification = clean_text(stream.get("classification"))
    status = stream.get("status")
    verified = bool(stream.get("verified") or stream.get("probe_ok")) and status in {200, 206, None}
    if stream.get("publishable") and verified:
        stream["playability"] = "verified"
        stream["observed_active"] = True
    elif stream.get("publishable") and classification in {"signed", "signed_runner_blocked"}:
        # URL có wsSecret còn hạn và đã được player trình duyệt phát sinh; runner có thể bị 403 riêng theo IP.
        stream["playability"] = "browser-observed"
        stream["observed_active"] = True
    else:
        stream["playability"] = "rejected"
        stream["observed_active"] = False
    stream.setdefault("quality", "")
    stream["http_status"] = status


def scan_window_metadata(url: str) -> dict[str, Any]:
    kickoff = parse_match_datetime(url)
    if not kickoff:
        return {
            "kickoff_iso": "",
            "minutes_to_kickoff": None,
            "scan_window_reason": "unknown-time-live",
        }
    now = datetime.now(VN_TZ)
    minutes = int(round((kickoff - now).total_seconds() / 60))
    return {
        "kickoff_iso": kickoff.isoformat(),
        "minutes_to_kickoff": minutes,
        "scan_window_reason": "time-window",
    }


async def scan_match(
    context: BrowserContext,
    url: str,
    args: argparse.Namespace,
    index: int,
    total: int,
) -> dict[str, Any]:
    print(f"\n{'=' * 78}", flush=True)
    print(f"[{index}/{total}] QUÉT XOILAC/MALAYSIANDIGEST: {url}", flush=True)
    print(f"{'=' * 78}", flush=True)

    page = await context.new_page()
    errors: list[str] = []
    started = time.monotonic()
    final_url = canonical_match_url(url)
    title = ""
    metadata: dict[str, Any] = derive_match_metadata(url)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=args.navigation_timeout * 1000)
        final_url = canonical_match_url(page.url)
        await page.wait_for_timeout(1200)
        title = await extract_page_title(page)
        metadata = await extract_match_page_data(page, final_url, title)
        print(
            f"↪️ {metadata['name']} | {metadata['time']} {metadata['date']} | "
            f"logo={bool(metadata['home_logo'])}/{bool(metadata['away_logo'])} | "
            f"nguồn={len(metadata['source_links'])}",
            flush=True,
        )
        if metadata.get("home_logo"):
            print(f"   🖼️ Logo chủ nhà: {metadata['home_logo']}", flush=True)
        if metadata.get("away_logo"):
            print(f"   🖼️ Logo đội khách: {metadata['away_logo']}", flush=True)
        print(
            "   🎙️ BLV/kênh: " + ", ".join(row["commentator"] for row in metadata["source_links"]),
            flush=True,
        )
    except Exception as exc:
        errors.append(f"metadata {type(exc).__name__}: {exc}")
    finally:
        try:
            await page.close()
        except Exception:
            pass

    source_links = list(metadata.get("source_links") or [])[: args.max_sources_per_match]
    source_results: list[dict[str, Any]] = []
    for source in source_links:
        source_results.append(await capture_source(context, source, args))

    streams: list[dict[str, Any]] = []
    seen_stream_keys: set[tuple[str, int, str]] = set()
    for source_result in source_results:
        for stream in source_result.get("streams", []):
            key = (
                clean_text(stream.get("url")),
                int(stream.get("source_index", 0)),
                clean_text(stream.get("commentator")),
            )
            if key in seen_stream_keys:
                continue
            seen_stream_keys.add(key)
            streams.append(stream)

    for stream in streams:
        annotate_multisource_playability(stream)

    publishable = sum(1 for stream in streams if stream.get("publishable"))
    placeholders = sum(1 for stream in streams if stream.get("placeholder_suspected"))
    signed = sum(1 for stream in streams if stream.get("has_secret"))
    print(
        f"🏁 Kết quả trận: nguồn={len(source_results)} | media={len(streams)} | "
        f"publishable={publishable} | signed={signed} | placeholder={placeholders}",
        flush=True,
    )

    timing = scan_window_metadata(final_url)
    return {
        "source": "xoilac",
        "url": final_url,
        "input_url": url,
        "final_url": final_url,
        **timing,
        "title": title,
        "match_name": metadata.get("name", derive_match_metadata(url)["name"]),
        "home_name": metadata.get("home_name", ""),
        "away_name": metadata.get("away_name", ""),
        "time": metadata.get("time", ""),
        "date": metadata.get("date", ""),
        "league": metadata.get("league", ""),
        "home_logo": metadata.get("home_logo", ""),
        "away_logo": metadata.get("away_logo", ""),
        "logo_candidates": metadata.get("logo_candidates", []),
        "source_links": source_links,
        "sources": source_results,
        "streams": streams,
        "errors": errors,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def m3u_attr(value: Any) -> str:
    return clean_text(value).replace("&", "&amp;").replace('"', "'")


def build_display_name(result: dict[str, Any], stream: dict[str, Any], index: int) -> str:
    name = clean_text(result.get("match_name")) or f"Xôi Lạc stream {index}"
    commentator = clean_text(stream.get("commentator"))
    time_value = clean_text(result.get("time"))
    date_value = clean_text(result.get("date"))
    kickoff = " ".join(part for part in (time_value, date_value) if part)
    display = f"[{kickoff}] {name}" if kickoff else name
    if commentator and commentator.lower() not in {"xoilac", "xôi lạc"}:
        display += f" [BLV {commentator}]"
    kind = clean_text(stream.get("kind")).upper()
    if kind in {"FLV", "M3U8", "MPD"}:
        display += f" [{kind}]"
    return display


def append_m3u_entry(
    universal: list[str],
    pipe: list[str],
    vlc: list[str],
    result: dict[str, Any],
    stream: dict[str, Any],
    index: int,
) -> None:
    display_name = build_display_name(result, stream, index)
    source_index = int(stream.get("source_index", 0))
    channel_id = stable_channel_id(
        str(result.get("final_url") or result.get("input_url")),
        index,
        source_index,
    )
    logo = clean_text(result.get("home_logo") or result.get("away_logo"))
    league = clean_text(result.get("league"))
    group_title = league or "Bóng đá"
    attributes = [
        f'tvg-id="{m3u_attr(channel_id)}"',
        f'tvg-name="{m3u_attr(display_name)}"',
        f'group-title="{m3u_attr(group_title)}"',
    ]
    if logo:
        attributes.append(f'tvg-logo="{m3u_attr(logo)}"')
    extinf = f"#EXTINF:-1 {' '.join(attributes)},{display_name}"
    url = clean_text(stream.get("url"))
    referer = clean_text(stream.get("referer") or stream.get("frame_url") or stream.get("source_url"))
    origin = clean_text(stream.get("origin"))
    if not origin and referer:
        parsed_referer = urlparse(referer)
        if parsed_referer.scheme and parsed_referer.netloc:
            origin = f"{parsed_referer.scheme}://{parsed_referer.netloc}"
    user_agent = clean_text(stream.get("user_agent")) or UA

    universal.append(extinf)
    if referer:
        universal.append(f"#EXTVLCOPT:http-referrer={referer}")
    if origin:
        universal.append(f"#EXTVLCOPT:http-origin={origin}")
    universal.append(f"#EXTVLCOPT:http-user-agent={user_agent}")
    universal.append("#EXTVLCOPT:http-reconnect=true")
    http_headers = {"User-Agent": user_agent}
    if referer:
        http_headers["Referer"] = referer
    if origin:
        http_headers["Origin"] = origin
    universal.append("#EXTHTTP:" + json.dumps(http_headers, ensure_ascii=False, separators=(",", ":")))
    universal.append(url)

    headers: list[tuple[str, str]] = [("User-Agent", user_agent)]
    if referer:
        headers.append(("Referer", referer))
    if origin:
        headers.append(("Origin", origin))
    pipe_query = "&".join(f"{key}={quote(value, safe=':/?=%')}" for key, value in headers)
    pipe.extend([extinf, f"{url}|{pipe_query}"])

    vlc.append(extinf)
    if referer:
        vlc.append(f"#EXTVLCOPT:http-referrer={referer}")
    if origin:
        vlc.append(f"#EXTVLCOPT:http-origin={origin}")
    vlc.append(f"#EXTVLCOPT:http-user-agent={user_agent}")
    vlc.append("#EXTVLCOPT:http-reconnect=true")
    vlc.append(url)


def write_simple_playlist(path: Path, rows: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
    lines = ["#EXTM3U"]
    for index, (result, stream) in enumerate(rows, start=1):
        dummy_pipe: list[str] = []
        dummy_vlc: list[str] = []
        append_m3u_entry(lines, dummy_pipe, dummy_vlc, result, stream, index)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def refresh_output_classification(stream: dict[str, Any]) -> None:
    """Tính lại hạn token ngay trước khi ghi file để không xuất wsSecret đã hết hạn."""
    url = clean_text(stream.get("url"))
    expiry = parse_signed_expiry(url)
    stream["expiry"] = expiry
    seconds_left = expiry.get("seconds_left")
    if stream.get("has_secret") and isinstance(seconds_left, int) and seconds_left <= 60:
        stream["publishable"] = False
        stream["classification"] = "expired"
        stream["classification_reason"] = "wsSecret hết hạn hoặc còn không quá 60 giây tại lúc ghi playlist."
    annotate_multisource_playability(stream)


def write_outputs(results: list[dict[str, Any]]) -> tuple[int, int]:
    universal = ["#EXTM3U"]
    pipe = ["#EXTM3U"]
    vlc = ["#EXTM3U"]
    preferred_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    verified_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    all_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []
    rejected_rows: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for result in results:
        for stream in result.get("streams", []):
            if not stream.get("url"):
                continue
            refresh_output_classification(stream)
            row = (result, stream)
            all_rows.append(row)
            if stream.get("publishable"):
                preferred_rows.append(row)
            else:
                rejected_rows.append(row)
            if (
                stream.get("status") == 200
                and stream.get("verified")
                and not stream.get("placeholder_suspected")
            ):
                verified_rows.append(row)

    seen: set[tuple[str, str, int]] = set()
    deduped_preferred: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for result, stream in preferred_rows:
        key = (
            clean_text(result.get("final_url")),
            clean_text(stream.get("url")),
            int(stream.get("source_index", 0)),
        )
        if key not in seen:
            seen.add(key)
            deduped_preferred.append((result, stream))

    for index, (result, stream) in enumerate(deduped_preferred, start=1):
        append_m3u_entry(universal, pipe, vlc, result, stream, index)

    summary = {
        "matches_scanned": len(results),
        "sources_scanned": sum(len(result.get("sources", [])) for result in results),
        "media_detected": len(all_rows),
        "main_playlist": len(deduped_preferred),
        "runner_verified": len(verified_rows),
        "signed": sum(1 for _, stream in all_rows if stream.get("has_secret")),
        "runner_blocked_signed": sum(
            1 for _, stream in all_rows if stream.get("classification") == "signed_runner_blocked"
        ),
        "placeholders_rejected": sum(
            1 for _, stream in all_rows if stream.get("placeholder_suspected")
        ),
        "dead_rejected": sum(
            1 for _, stream in all_rows if stream.get("classification") == "dead"
        ),
        "matches_with_home_logo": sum(1 for result in results if result.get("home_logo")),
        "matches_with_away_logo": sum(1 for result in results if result.get("away_logo")),
        "sources_with_commentator": sum(
            1
            for result in results
            for source in result.get("sources", [])
            if clean_text(source.get("commentator"))
        ),
    }

    OUTPUT_DEBUG.write_text(
        json.dumps(
            {
                "version": VERSION,
                "generated_at": datetime.now(VN_TZ).isoformat(),
                "summary": summary,
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    OUTPUT_M3U.write_text("\n".join(universal) + "\n", encoding="utf-8")
    OUTPUT_PIPE_M3U.write_text("\n".join(pipe) + "\n", encoding="utf-8")
    OUTPUT_VLC_M3U.write_text("\n".join(vlc) + "\n", encoding="utf-8")
    if read_env_bool("XOILAC_WRITE_AUDIT_M3U", False):
        write_simple_playlist(OUTPUT_VERIFIED_M3U, verified_rows)
        write_simple_playlist(OUTPUT_ALL_M3U, all_rows)
        write_simple_playlist(OUTPUT_REJECTED_M3U, rejected_rows)
    return len({clean_text(result.get("final_url")) for result, _ in deduped_preferred}), len(deduped_preferred)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Nguồn Xôi Lạc/XoilacZ/MalaysianDigest: lấy logo đội, tên BLV, quét từng /link/N, "
            "ưu tiên URL FLV có wsSecret và loại player quảng cáo type/8 khỏi M3U chính."
        )
    )
    parser.add_argument("urls", nargs="*", help="URL trang trận hoặc trang chủ")
    parser.add_argument("--headed", action="store_true", help="Hiện cửa sổ Chromium")
    parser.add_argument("--cdp", default=os.getenv("XOILAC_CDP_URL", ""), help="Kết nối Chrome thật qua CDP")
    parser.add_argument(
        "--wait",
        "--source-wait",
        dest="source_wait_seconds",
        type=int,
        default=int(os.getenv("XOILAC_SOURCE_WAIT_SECONDS", os.getenv("XOILAC_WAIT_SECONDS", "12"))),
        help="Thời gian chờ media cho mỗi BLV/kênh; --wait được giữ để tương thích bản cũ.",
    )
    parser.add_argument("--home-wait", type=int, default=int(os.getenv("XOILAC_HOME_WAIT_SECONDS", "5")))
    parser.add_argument("--after-first-wait", type=int, default=int(os.getenv("XOILAC_AFTER_FIRST_WAIT", "2")))
    parser.add_argument("--navigation-timeout", type=int, default=int(os.getenv("XOILAC_NAVIGATION_TIMEOUT", "35")))
    parser.add_argument("--verify-timeout", type=int, default=int(os.getenv("XOILAC_VERIFY_TIMEOUT", "8")))
    parser.add_argument("--no-verify", dest="verify", action="store_false")
    parser.set_defaults(verify=read_env_bool("XOILAC_VERIFY_STREAMS", True))
    parser.add_argument("--max-matches", type=int, default=int(os.getenv("XOILAC_MAX_MATCHES", "5")))
    parser.add_argument("--max-sources-per-match", type=int, default=int(os.getenv("XOILAC_MAX_SOURCES_PER_MATCH", "4")))
    parser.add_argument("--token-refresh-attempts", type=int, default=int(os.getenv("XOILAC_TOKEN_REFRESH_ATTEMPTS", "2")))
    parser.add_argument("--min-token-seconds", type=int, default=int(os.getenv("XOILAC_MIN_TOKEN_SECONDS", "600")))
    parser.add_argument("--past-minutes", type=int, default=int(os.getenv("XOILAC_SCAN_PAST_MINUTES", "150")))
    parser.add_argument("--future-minutes", type=int, default=int(os.getenv("XOILAC_SCAN_FUTURE_MINUTES", "240")))
    return parser.parse_args()


async def launch_or_connect(playwright: Any, args: argparse.Namespace) -> tuple[Browser, BrowserContext, bool]:
    if args.cdp:
        print(f"🔌 Kết nối Chrome thật qua CDP: {args.cdp}", flush=True)
        browser = await playwright.chromium.connect_over_cdp(args.cdp)
        if not browser.contexts:
            raise RuntimeError("Chrome CDP không có BrowserContext.")
        return browser, browser.contexts[0], False

    launch_options: dict[str, Any] = {
        "headless": not args.headed,
        "args": [
            "--disable-blink-features=AutomationControlled",
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    }
    executable = os.getenv("PLAYWRIGHT_CHROMIUM_EXECUTABLE", "").strip()
    if executable and Path(executable).is_file():
        launch_options["executable_path"] = executable
        print(f"ℹ️ Dùng Chromium/Chrome hệ thống: {executable}", flush=True)

    browser = await playwright.chromium.launch(**launch_options)
    context = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=UA,
        locale="vi-VN",
        timezone_id="Asia/Ho_Chi_Minh",
        ignore_https_errors=True,
        service_workers="block",
        extra_http_headers={"Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"},
    )
    return browser, context, True


async def async_main() -> int:
    args = parse_args()
    args.source_wait_seconds = max(4, min(args.source_wait_seconds, 60))
    args.home_wait = max(1, min(args.home_wait, 60))
    args.after_first_wait = max(0, min(args.after_first_wait, 30))
    args.navigation_timeout = max(10, min(args.navigation_timeout, 120))
    args.verify_timeout = max(3, min(args.verify_timeout, 30))
    args.max_matches = max(1, min(args.max_matches, 30))
    args.max_sources_per_match = max(1, min(args.max_sources_per_match, 8))
    args.token_refresh_attempts = max(1, min(args.token_refresh_attempts, 3))
    args.min_token_seconds = max(60, min(args.min_token_seconds, 7200))

    print(f"🥷 KHỞI ĐỘNG XÔI LẠC STREAM SCANNER - {VERSION}", flush=True)
    print(
        "ℹ️ Quét từng link BLV; M3U chính loại type/8-live2 không wsSecret, "
        "nhưng vẫn lưu toàn bộ candidate/rejected để audit.",
        flush=True,
    )

    async with async_playwright() as playwright:
        browser, context, owns_browser = await launch_or_connect(playwright, args)
        discovery_rows: list[dict[str, Any]] = []
        try:
            supplied = [value.strip() for value in args.urls if value.strip()]
            targets: list[str] = []
            if supplied:
                for value in supplied:
                    if MATCH_URL_RE.search(urlparse(value).path):
                        targets.append(canonical_match_url(value))
                    else:
                        found, discovery = await discover_targets(context, value, args)
                        targets.extend(found)
                        discovery_rows.append(discovery)
            else:
                for home_url in HOME_URLS:
                    try:
                        found, discovery = await discover_targets(context, home_url, args)
                    except Exception as exc:
                        print(f"⚠️ Miền Xôi Lạc lỗi {home_url}: {type(exc).__name__}: {exc}", flush=True)
                        continue
                    discovery_rows.append(discovery)
                    if found:
                        targets.extend(found)
                        print(f"✅ Chọn miền Xôi Lạc: {home_url} | trận={len(found)}", flush=True)
                        break

            unique_targets: list[str] = []
            for target in targets:
                canonical = canonical_match_url(target)
                if canonical not in unique_targets:
                    unique_targets.append(canonical)
            targets = unique_targets[: args.max_matches]

            if not targets:
                print("❌ Không tìm thấy URL trận để quét.", flush=True)
                write_outputs([])
                return 2

            print(
                f"🚀 Quét {len(targets)} trận; tối đa {args.max_sources_per_match} BLV/kênh mỗi trận.",
                flush=True,
            )
            results: list[dict[str, Any]] = []
            for index, target in enumerate(targets, start=1):
                results.append(await scan_match(context, target, args, index, len(targets)))

            if discovery_rows:
                for result in results:
                    result.setdefault("discovery", discovery_rows)

            matches, links = write_outputs(results)
            summary = json.loads(OUTPUT_DEBUG.read_text(encoding="utf-8"))["summary"]
            print("\n📊 TỔNG KẾT", flush=True)
            print(
                f"   Trận={summary['matches_scanned']} | nguồn={summary['sources_scanned']} | "
                f"media={summary['media_detected']} | M3U chính={summary['main_playlist']}",
                flush=True,
            )
            print(
                f"   signed={summary['signed']} | runner-verified={summary['runner_verified']} | "
                f"runner-blocked-signed={summary['runner_blocked_signed']}",
                flush=True,
            )
            print(
                f"   loại placeholder={summary['placeholders_rejected']} | "
                f"logo chủ/khách={summary['matches_with_home_logo']}/{summary['matches_with_away_logo']}",
                flush=True,
            )
            if links:
                print(f"\n🎉 Xuất {links} link ưu tiên từ {matches} trận.", flush=True)
            else:
                print("\n⚠️ Không có link đủ điều kiện vào M3U chính; xem all_candidates/rejected.", flush=True)
            for path in (
                OUTPUT_M3U,
                OUTPUT_PIPE_M3U,
                OUTPUT_VLC_M3U,
                OUTPUT_VERIFIED_M3U,
                OUTPUT_ALL_M3U,
                OUTPUT_REJECTED_M3U,
                OUTPUT_DEBUG,
            ):
                if path.exists():
                    print(f"📄 {path.name}: {path}", flush=True)
            return 0 if links else 1
        finally:
            if owns_browser:
                await context.close()
                await browser.close()


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nĐã hủy.", flush=True)
        return 130
    except Exception as exc:
        print(f"❌ Lỗi nghiêm trọng: {type(exc).__name__}: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
