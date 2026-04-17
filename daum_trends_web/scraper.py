"""Daum 모바일 메인에서 실시간 트렌드를 추출한다."""

from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DAUM_TRENDS_URL = "https://m.daum.net/"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_TTL_SECONDS = 60
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)


class TrendFetchError(RuntimeError):
    """네트워크 요청 실패."""


class TrendParseError(RuntimeError):
    """HTML 파싱 실패."""


@dataclass
class TrendItem:
    rank: int
    keyword: str
    status: str
    url: str


@dataclass
class TrendSnapshot:
    source_url: str
    updated_at_label: str
    retrieved_at: str
    notice: str
    items: List[TrendItem]

    def to_dict(self) -> Dict[str, object]:
        return {
            "source_url": self.source_url,
            "updated_at_label": self.updated_at_label,
            "retrieved_at": self.retrieved_at,
            "notice": self.notice,
            "item_count": len(self.items),
            "items": [asdict(item) for item in self.items],
        }


class _DaumTrendParser(HTMLParser):
    """확장된 실시간 트렌드 레이어만 읽어 들인다."""

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self._div_depth = 0
        self._layer_depth = None  # type: Optional[int]
        self._in_list = False
        self._in_item = False
        self._current_field = None  # type: Optional[str]
        self._current_item = {}  # type: Dict[str, str]
        self.updated_at_label = ""
        self.notice = ""
        self.items = []  # type: List[TrendItem]

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        attrs_map = dict(attrs)
        classes = set(attrs_map.get("class", "").split())

        if tag == "div":
            self._div_depth += 1
            if self._layer_depth is None and "layer_trendrank" in classes:
                self._layer_depth = self._div_depth
            return

        if not self._inside_layer():
            return

        if tag == "p" and "info_trendrank" in classes:
            self._current_field = "updated_at_label"
        elif tag == "p" and "desc_tip" in classes:
            self._current_field = "notice"
        elif tag == "ol" and "list_trendrank" in classes:
            self._in_list = True
        elif self._in_list and tag == "li":
            self._in_item = True
            self._current_item = {}
        elif self._in_item and tag == "a" and "link_item" in classes:
            self._current_item["url"] = attrs_map.get("href", "")
        elif self._in_item and tag == "em" and "num_rank" in classes:
            self._current_field = "rank"
        elif self._in_item and tag == "strong" and "tit_item" in classes:
            self._current_field = "keyword"
        elif self._in_item and tag == "span" and "ico_newmtg" in classes:
            self._current_field = "status"

    def handle_endtag(self, tag: str) -> None:
        if tag == "div":
            if self._layer_depth is not None and self._div_depth == self._layer_depth:
                self._layer_depth = None
                self._in_list = False
                self._in_item = False
                self._current_field = None
            self._div_depth = max(0, self._div_depth - 1)
            return

        if not self._inside_layer():
            return

        if tag in {"p", "em", "strong", "span"}:
            self._current_field = None
        elif tag == "li" and self._in_item:
            item = self._build_item(self._current_item)
            if item is not None:
                self.items.append(item)
            self._current_item = {}
            self._in_item = False
        elif tag == "ol":
            self._in_list = False

    def handle_data(self, data: str) -> None:
        if not self._inside_layer() or self._current_field is None:
            return

        chunk = data.strip()
        if not chunk:
            return

        if self._current_field == "updated_at_label":
            self.updated_at_label += self._join_text(self.updated_at_label, chunk)
        elif self._current_field == "notice":
            self.notice += self._join_text(self.notice, chunk)
        elif self._in_item:
            existing = self._current_item.get(self._current_field, "")
            self._current_item[self._current_field] = (
                existing + self._join_text(existing, chunk)
            )

    def _inside_layer(self) -> bool:
        return self._layer_depth is not None and self._div_depth >= self._layer_depth

    @staticmethod
    def _join_text(existing: str, chunk: str) -> str:
        if not existing:
            return chunk
        return " " + chunk

    @staticmethod
    def _build_item(raw_item: Dict[str, str]) -> Optional[TrendItem]:
        rank_match = re.search(r"\d+", raw_item.get("rank", ""))
        keyword = raw_item.get("keyword", "").strip()
        if rank_match is None or not keyword:
            return None

        status = raw_item.get("status", "").replace(",", "").strip() or "변동없음"
        url = raw_item.get("url", "").strip()

        return TrendItem(
            rank=int(rank_match.group(0)),
            keyword=keyword,
            status=status,
            url=url,
        )


def fetch_trend_html(
    url: str = DAUM_TRENDS_URL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "no-cache",
        },
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            content_type = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(content_type, errors="replace")
    except HTTPError as exc:
        raise TrendFetchError("Daum 응답이 비정상적입니다: HTTP {0}".format(exc.code))
    except URLError as exc:
        raise TrendFetchError("Daum에 연결하지 못했습니다: {0}".format(exc.reason))


def parse_trend_snapshot(html: str, source_url: str = DAUM_TRENDS_URL) -> TrendSnapshot:
    parser = _DaumTrendParser()
    parser.feed(html)
    parser.close()

    if not parser.items:
        raise TrendParseError("실시간 트렌드 목록을 찾지 못했습니다.")

    updated_at_label = parser.updated_at_label.strip()
    if not updated_at_label:
        raise TrendParseError("기준 시각을 찾지 못했습니다.")

    return TrendSnapshot(
        source_url=source_url,
        updated_at_label=updated_at_label,
        retrieved_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        notice=parser.notice.strip(),
        items=sorted(parser.items, key=lambda item: item.rank),
    )


def fetch_trend_snapshot() -> TrendSnapshot:
    return parse_trend_snapshot(fetch_trend_html())


class TrendCache:
    """짧은 TTL 캐시로 과도한 호출을 막는다."""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.ttl = timedelta(seconds=ttl_seconds)
        self._lock = threading.Lock()
        self._snapshot = None  # type: Optional[TrendSnapshot]
        self._expires_at = None  # type: Optional[datetime]

    def get_snapshot(self, force_refresh: bool = False) -> TrendSnapshot:
        with self._lock:
            now = datetime.now().astimezone()
            if (
                force_refresh
                or self._snapshot is None
                or self._expires_at is None
                or now >= self._expires_at
            ):
                self._snapshot = fetch_trend_snapshot()
                self._expires_at = now + self.ttl
            return self._snapshot

