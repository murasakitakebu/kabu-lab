"""J-Quants → DuckDB の取得手順（初回フル取得・差分更新で共通）。

方針
- 株価・財務サマリーは「1 日分の全銘柄」を 1 リクエストで取る（銘柄ごとより桁違いに少ない）。
- 日付単位の取得が成功したら fetch_log に残し、次回以降はその日を飛ばす（中断しても再開できる）。
- 契約プランの範囲外（4xx）になった日はエラーとして記録して先へ進む。認証エラーは即中止。
- 株式分割があると、J-Quants は過去の調整後価格を遡って書き換える。差分更新で
  AdjFactor≠1 の銘柄を見つけたら、その銘柄の全期間を取り直す。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from requests.exceptions import HTTPError

from core.data.jquants import JQuantsClient, Plan
from core.data.store import Store

logger = logging.getLogger(__name__)

#: プランごとの取得可能期間：(遡れる年数, 直近の除外日数)
PLAN_WINDOWS: dict[Plan, tuple[int, int]] = {
    Plan.FREE: (2, 12 * 7),
    Plan.LIGHT: (5, 0),
    Plan.STANDARD: (10, 0),
    Plan.PREMIUM: (20, 0),
}

#: 取引カレンダーで「立会あり」を表す HolDiv（1: 営業日, 2: 半日立会）
TRADING_HOLDIVS = {"1", "2"}

#: これらの HTTP ステータスはプラン・入力の問題なので、その日を飛ばして続ける
SKIPPABLE_STATUS = {400, 403, 404}


def plan_window(plan: Plan, today: date) -> tuple[date, date]:
    """プランで取得できる期間（両端を含む）。境界で外れないよう開始側を 1 日内側にする。"""
    years, embargo_days = PLAN_WINDOWS[plan]
    try:
        start = today.replace(year=today.year - years)
    except ValueError:  # 2/29
        start = today.replace(year=today.year - years, day=28)
    return start + timedelta(days=1), today - timedelta(days=embargo_days)


@dataclass
class SyncResult:
    """取得結果のまとめ。"""

    fetched: dict[str, int] = field(default_factory=dict)  # job -> 取得した日数
    rows: dict[str, int] = field(default_factory=dict)  # job -> 保存行数
    errors: list[str] = field(default_factory=list)
    requests: int = 0

    def add(self, job: str, rows: int) -> None:
        self.fetched[job] = self.fetched.get(job, 0) + 1
        self.rows[job] = self.rows.get(job, 0) + rows


class Loader:
    """取得手順。`client` と `store` を受け取るだけなので、テストでは偽物を渡せる。"""

    def __init__(
        self,
        client: JQuantsClient,
        store: Store,
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.store = store
        self.progress = progress or logger.info

    # --- 個別ジョブ ---------------------------------------------------------

    def _run(
        self, job: str, result: SyncResult, fetch: Callable[[], pd.DataFrame], save: Callable, **params
    ) -> bool:
        """1 ジョブを実行して fetch_log に残す。続行可能なエラーなら False を返す。"""
        before = self.client.request_count
        try:
            with self.store.fetch_job(job, **params) as rec:
                try:
                    df = fetch()
                    rec.rows = save(df)
                finally:
                    rec.requests = self.client.request_count - before
        except HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status not in SKIPPABLE_STATUS:
                raise
            msg = f"{job} {params}: HTTP {status}"
            result.errors.append(msg)
            logger.warning(msg)
            return False
        finally:
            result.requests += self.client.request_count - before
        result.add(job, rec.rows)
        return True

    def sync_calendar(self, start: date, end: date, result: SyncResult) -> list[date]:
        """取引カレンダーを取得・保存し、期間内の立会日を返す。"""
        self._run(
            "trading_calendar",
            result,
            lambda: self.client.trading_calendar(start, end),
            self.store.upsert_trading_calendar,
            start=start,
            end=end,
        )
        return self.trading_days(start, end)

    def trading_days(self, start: date, end: date) -> list[date]:
        """保存済みカレンダーから期間内の立会日を返す。"""
        rows = self.store.con.execute(
            "SELECT date FROM trading_calendar WHERE date BETWEEN ? AND ?"
            " AND holiday_division IN (SELECT unnest(?)) ORDER BY date",
            [start, end, sorted(TRADING_HOLDIVS)],
        ).fetchall()
        return [r[0] for r in rows]

    def sync_listed_info(self, dates: Iterable[date], result: SyncResult) -> None:
        """指定日ごとの上場銘柄一覧スナップショットを取る（取得済みの日は飛ばす）。"""
        done = self.store.completed_dates("listed_info")
        for d in dates:
            if d in done:
                continue
            self._run(
                "listed_info",
                result,
                lambda d=d: self.client.listed_info(on=d),
                lambda df, d=d: self.store.upsert_listed_info(df, as_of=d),
                date=d,
            )

    def sync_by_date(self, job: str, days: list[date], result: SyncResult) -> list[date]:
        """株価（daily_bars）または財務サマリー（fin_summary）を日付ごとに取る。

        戻り値は今回新たに取得できた日付。
        """
        fetchers = {
            "daily_bars": (
                lambda d: self.client.daily_bars(on=d),
                self.store.upsert_daily_bars,
            ),
            "fin_summary": (
                lambda d: self.client.fin_summary(disclosed_on=d),
                self.store.upsert_fin_summary,
            ),
        }
        fetch, save = fetchers[job]
        done = self.store.completed_dates(job)
        todo = [d for d in days if d not in done]
        self.progress(f"{job}: {len(todo)} 日分を取得（取得済み {len(days) - len(todo)} 日）")
        fetched: list[date] = []
        t0 = time.monotonic()
        for i, d in enumerate(todo, 1):
            if self._run(job, result, lambda d=d: fetch(d), save, date=d):
                fetched.append(d)
            if i % 20 == 0 or i == len(todo):
                elapsed = time.monotonic() - t0
                eta = elapsed / i * (len(todo) - i)
                self.progress(f"{job}: {i}/{len(todo)} ({d}) 残り約 {eta / 60:.0f} 分")
        return fetched

    def refresh_split_adjusted(self, since: list[date], start: date, end: date, result: SyncResult) -> None:
        """`since` の日付に AdjFactor≠1（分割・併合）があった銘柄の全期間を取り直す。"""
        if not since:
            return
        codes = [
            r[0]
            for r in self.store.con.execute(
                "SELECT DISTINCT code FROM daily_bars WHERE date IN (SELECT unnest(?))"
                " AND adj_factor IS NOT NULL AND adj_factor <> 1 ORDER BY code",
                [since],
            ).fetchall()
        ]
        if codes:
            self.progress(f"分割・併合のあった {len(codes)} 銘柄の調整後価格を取り直します")
        for code in codes:
            self._run(
                "daily_bars_by_code",
                result,
                lambda code=code: self.client.daily_bars(code, start=start, end=end),
                self.store.upsert_daily_bars,
                code=code,
                start=start,
                end=end,
            )

    # --- まとめ -------------------------------------------------------------

    def sync(
        self,
        start: date,
        end: date,
        *,
        include_fin: bool = True,
        refresh_splits: bool = False,
        history_start: date | None = None,
        limit_days: int | None = None,
    ) -> SyncResult:
        """期間内のデータをまとめて取得する（取得済みの日は飛ばす）。

        Args:
            start, end: 取得期間（両端を含む）
            include_fin: 財務サマリーも取るか
            refresh_splits: 新たに取った日に分割があれば、その銘柄の全期間を取り直すか
            history_start: 分割時に取り直す期間の開始（省略時は start）
            limit_days: 動作確認用。株価・財務を先頭からこの日数だけ取る
        """
        result = SyncResult()
        days = self.sync_calendar(start, end, result)
        if not days:
            self.progress("期間内に立会日がありません")
            return result
        if limit_days is not None:
            days = days[:limit_days]

        # 銘柄一覧：各月の最初の立会日 + 最終日（上場廃止銘柄を拾うため月次で持つ）
        firsts: dict[tuple[int, int], date] = {}
        for d in days:
            firsts.setdefault((d.year, d.month), d)
        self.sync_listed_info(sorted({*firsts.values(), days[-1]}), result)

        new_bar_days = self.sync_by_date("daily_bars", days, result)
        if include_fin:
            self.sync_by_date("fin_summary", days, result)
        if refresh_splits:
            self.refresh_split_adjusted(new_bar_days, history_start or start, end, result)
        return result
