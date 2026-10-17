"""J-Quants API V2 クライアントのラッパー。

`jquants-api-client` の `ClientV2` は全リクエストを `_get` 経由で発行するので、
そこを上書きして以下を足している。

- プランごとのレートリミットに合わせたスロットリング（スレッドセーフ）
- 429 / 5xx / 通信エラー時の指数バックオフ付きリトライ（Retry-After を尊重）

`ClientV2` の `*_range` 系メソッドはスレッド並列でリクエストするが、
スロットリングはプロセス内で共有されるので上限を超えない。

銘柄コードは J-Quants V2 の 5 桁形式（例: トヨタ = "72030"）に統一する。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

import pandas as pd
import requests
from dotenv import load_dotenv
from jquantsapi import ClientV2
from requests.adapters import HTTPAdapter
from requests.exceptions import HTTPError

logger = logging.getLogger(__name__)


class Plan(StrEnum):
    """J-Quants の契約プラン。"""

    FREE = "free"
    LIGHT = "light"
    STANDARD = "standard"
    PREMIUM = "premium"


#: プランごとのレートリミット（リクエスト/分）。docs/requirements.md §8 参照。
RATE_LIMITS: dict[Plan, int] = {
    Plan.FREE: 5,
    Plan.LIGHT: 60,
    Plan.STANDARD: 120,
    Plan.PREMIUM: 500,
}

#: リトライ対象の HTTP ステータス
RETRYABLE_STATUS: frozenset[int] = frozenset({429, 500, 502, 503, 504})


class RateLimiter:
    """リクエスト間隔を一定以上空けるスロットリング。

    上限 `requests_per_minute` に `safety` を掛けた速度で、リクエストを等間隔に並べる。
    バーストを許さない分保守的だが、レートリミットの数え方（固定窓/スライド窓）に
    依存せず確実に上限を下回る。
    """

    def __init__(
        self,
        requests_per_minute: float,
        *,
        safety: float = 0.9,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        if not 0 < safety <= 1:
            raise ValueError("safety must be in (0, 1]")
        self.interval = 60.0 / (requests_per_minute * safety)
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_at: float | None = None

    def acquire(self) -> None:
        """次のリクエストを出してよい時刻まで待つ。"""
        with self._lock:
            now = self._clock()
            if self._next_at is None or now >= self._next_at:
                self._next_at = now + self.interval
                return
            wait = self._next_at - now
            self._next_at += self.interval
        # ロック外で待つ（他スレッドは自分の枠を予約して並ぶ）
        self._sleep(wait)

    def penalize(self, seconds: float) -> None:
        """429 を受けたときなどに、以降のリクエストを `seconds` 秒後まで止める。"""
        with self._lock:
            resume_at = self._clock() + seconds
            if self._next_at is None or self._next_at < resume_at:
                self._next_at = resume_at


@dataclass(frozen=True)
class RetryPolicy:
    """リトライ設定。待ち時間は base_delay * 2**(試行回数-1)、上限 max_delay 秒。"""

    max_attempts: int = 5
    base_delay: float = 2.0
    max_delay: float = 120.0

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        """`attempt` 回目（1始まり）の失敗後に待つ秒数。"""
        backoff = min(self.max_delay, self.base_delay * 2 ** (attempt - 1))
        if retry_after is not None:
            return max(backoff, min(retry_after, self.max_delay))
        return backoff


def _parse_retry_after(resp: requests.Response | None) -> float | None:
    if resp is None:
        return None
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class ThrottledClientV2(ClientV2):
    """スロットリングとリトライを組み込んだ `ClientV2`。"""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        rate_limiter: RateLimiter,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        super().__init__(api_key=api_key)
        self.rate_limiter = rate_limiter
        self.retry = retry or RetryPolicy()
        self._sleep = sleep
        self.request_count = 0
        self._count_lock = threading.Lock()

    def _request_session(
        self,
        status_forcelist: list[int] | None = None,
        allowed_methods: list[str] | None = None,
    ) -> requests.Session:
        # urllib3 側のリトライは無効化し、_get のリトライに一本化する
        # （urllib3 のリトライはスロットリングを通らないため）
        if self._session is None:
            adapter = HTTPAdapter(
                pool_connections=self.MAX_WORKERS + 10,
                pool_maxsize=self.MAX_WORKERS + 10,
                max_retries=0,
            )
            self._session = requests.Session()
            self._session.mount("https://", adapter)
        return self._session

    def _get(self, url: str, params: dict[str, Any] | None = None) -> requests.Response:
        attempt = 0
        while True:
            attempt += 1
            self.rate_limiter.acquire()
            with self._count_lock:
                self.request_count += 1
            try:
                return super()._get(url, params=params)
            except HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                if status not in RETRYABLE_STATUS or attempt >= self.retry.max_attempts:
                    raise
                wait = self.retry.delay(attempt, _parse_retry_after(e.response))
                if status == 429:
                    self.rate_limiter.penalize(wait)
                reason = f"HTTP {status}"
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt >= self.retry.max_attempts:
                    raise
                wait = self.retry.delay(attempt)
                reason = type(e).__name__
            logger.warning(
                "J-Quants %s: %s (attempt %d/%d), retry in %.1fs",
                url,
                reason,
                attempt,
                self.retry.max_attempts,
                wait,
            )
            self._sleep(wait)


def normalize_code(code: str | int) -> str:
    """銘柄コードを J-Quants V2 の 5 桁形式にそろえる。

    4 桁（"7203"、英字入り "130A" も可）なら末尾に "0" を付ける。5 桁はそのまま。
    """
    s = str(code).strip().upper()
    if len(s) == 4:
        return s + "0"
    if len(s) == 5:
        return s
    raise ValueError(f"invalid security code: {code!r}")


def display_code(code: str) -> str:
    """5 桁コードを画面表示用の 4 桁に戻す（末尾が "0" の場合のみ）。"""
    code = normalize_code(code)
    return code[:4] if code.endswith("0") else code


def _fmt(d: date | None) -> str:
    return d.isoformat() if d is not None else ""


def _normalize_code_column(df: pd.DataFrame) -> pd.DataFrame:
    if "Code" in df.columns and not df.empty:
        df = df.copy()
        df["Code"] = df["Code"].astype(str).map(normalize_code)
    return df


class JQuantsClient:
    """Kabu Lab が使う J-Quants エンドポイントの薄いファサード。

    日付は `datetime.date` で受け取り、返り値の DataFrame は V2 のフィールド名のまま返す
    （型変換・スキーマへの写像は `core.data.store` 側で行う）。`Code` 列は 5 桁に統一する。
    その他のエンドポイントは `.raw`（`ThrottledClientV2`）から直接呼べる。
    """

    def __init__(self, raw: ThrottledClientV2) -> None:
        self.raw = raw

    @classmethod
    def from_env(
        cls,
        *,
        plan: Plan | str | None = None,
        retry: RetryPolicy | None = None,
    ) -> JQuantsClient:
        """`.env` / 環境変数（JQUANTS_API_KEY, JQUANTS_PLAN）から生成する。"""
        load_dotenv()
        api_key = os.environ.get("JQUANTS_API_KEY", "")
        if not api_key:
            raise ValueError("JQUANTS_API_KEY が設定されていません（.env を確認）")
        plan = Plan(plan or os.environ.get("JQUANTS_PLAN") or Plan.FREE)
        limiter = RateLimiter(RATE_LIMITS[plan])
        logger.info("J-Quants plan=%s, interval=%.2fs", plan, limiter.interval)
        return cls(ThrottledClientV2(api_key, rate_limiter=limiter, retry=retry))

    @property
    def request_count(self) -> int:
        """これまでに発行したリクエスト数（リトライ含む）。"""
        return self.raw.request_count

    def listed_info(self, on: date | None = None, code: str = "") -> pd.DataFrame:
        """上場銘柄一覧（/equities/master）。`on` 省略時は最新。"""
        df = self.raw.get_eq_master(code=normalize_code(code) if code else "", date=_fmt(on))
        return _normalize_code_column(df)

    def daily_bars(
        self,
        code: str = "",
        start: date | None = None,
        end: date | None = None,
        on: date | None = None,
    ) -> pd.DataFrame:
        """株価四本値（/equities/bars/daily）。銘柄×期間 または 全銘柄×1日 で取得する。"""
        if not code and on is None:
            raise ValueError("code か on のどちらかを指定してください")
        df = self.raw.get_eq_bars_daily(
            code=normalize_code(code) if code else "",
            from_yyyymmdd=_fmt(start),
            to_yyyymmdd=_fmt(end),
            date_yyyymmdd=_fmt(on),
        )
        return _normalize_code_column(df)

    def fin_summary(self, code: str = "", disclosed_on: date | None = None) -> pd.DataFrame:
        """決算短信サマリ（/fins/summary）。銘柄指定 または 開示日指定。"""
        if not code and disclosed_on is None:
            raise ValueError("code か disclosed_on のどちらかを指定してください")
        df = self.raw.get_fin_summary(
            code=normalize_code(code) if code else "", date_yyyymmdd=_fmt(disclosed_on)
        )
        return _normalize_code_column(df)

    def dividends(
        self,
        code: str = "",
        start: date | None = None,
        end: date | None = None,
        on: date | None = None,
    ) -> pd.DataFrame:
        """配当金情報（/fins/dividend）。"""
        if not code and on is None and start is None:
            raise ValueError("code・on・start のいずれかを指定してください")
        df = self.raw.get_fin_dividend(
            code=normalize_code(code) if code else "",
            from_yyyymmdd=_fmt(start),
            to_yyyymmdd=_fmt(end),
            date_yyyymmdd=_fmt(on),
        )
        return _normalize_code_column(df)

    def topix(self, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        """TOPIX 四本値（/indices/bars/daily/topix）。"""
        return self.raw.get_idx_bars_daily_topix(from_yyyymmdd=_fmt(start), to_yyyymmdd=_fmt(end))

    def index_bars(
        self, index_code: str, start: date | None = None, end: date | None = None
    ) -> pd.DataFrame:
        """指数四本値（/indices/bars/daily）。"""
        return self.raw.get_idx_bars_daily(
            code=index_code, from_yyyymmdd=_fmt(start), to_yyyymmdd=_fmt(end)
        )

    def trading_calendar(self, start: date | None = None, end: date | None = None) -> pd.DataFrame:
        """取引カレンダー（/markets/calendar）。"""
        return self.raw.get_mkt_calendar(from_yyyymmdd=_fmt(start), to_yyyymmdd=_fmt(end))
