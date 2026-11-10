"""core.data.loader のテスト（偽クライアント＋メモリ DB）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import pandas as pd
import pytest
import requests
from requests.exceptions import HTTPError

from core.data.jquants import Plan
from core.data.loader import Loader, plan_window
from core.data.store import Store

D0 = date(2024, 9, 2)  # 月曜


class FakeClient:
    """日付指定で 1 リクエストずつ返す偽 J-Quants クライアント。"""

    def __init__(self, days: int = 10, fail_on: set[date] | None = None, split: date | None = None):
        self.start = D0
        self.days = days
        self.fail_on = fail_on or set()
        self.split = split
        self.request_count = 0
        self.calls: list[tuple[str, object]] = []

    def _req(self, name: str, arg: object) -> None:
        self.request_count += 1
        self.calls.append((name, arg))

    def trading_calendar(self, start: date, end: date) -> pd.DataFrame:
        self._req("calendar", (start, end))
        rows = []
        d = start
        while d <= end:
            rows.append({"Date": pd.Timestamp(d), "HolDiv": "1" if d.weekday() < 5 else "0"})
            d += timedelta(days=1)
        return pd.DataFrame(rows)

    def listed_info(self, on: date | None = None, code: str = "") -> pd.DataFrame:
        self._req("listed", on)
        return pd.DataFrame([{"Code": "72030", "CoName": "トヨタ自動車"}])

    def _bar(self, code: str, d: date) -> dict[str, object]:
        factor = 0.2 if (self.split is not None and d == self.split and code == "72030") else 1.0
        return {
            "Date": pd.Timestamp(d),
            "Code": code,
            "C": 100.0,
            "AdjC": 100.0,
            "AdjFactor": factor,
        }

    def daily_bars(
        self,
        code: str = "",
        start: date | None = None,
        end: date | None = None,
        on: date | None = None,
    ) -> pd.DataFrame:
        self._req("bars", on or code)
        if on in self.fail_on:
            resp = requests.Response()
            resp.status_code = 400
            raise HTTPError("400", response=resp)
        if on is not None:
            return pd.DataFrame([self._bar("72030", on), self._bar("79740", on)])
        return pd.DataFrame([self._bar(code, start)])

    def fin_summary(self, code: str = "", disclosed_on: date | None = None) -> pd.DataFrame:
        self._req("fin", disclosed_on)
        return pd.DataFrame()


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store(":memory:")
    yield s
    s.close()


def test_plan_window_free() -> None:
    start, end = plan_window(Plan.FREE, date(2026, 11, 11))
    assert start == date(2024, 11, 12)
    assert end == date(2026, 8, 19)  # 12 週間前


def test_plan_window_leap_day() -> None:
    start, _ = plan_window(Plan.LIGHT, date(2028, 2, 29))
    assert start == date(2023, 3, 1)


def test_sync_fetches_trading_days_only(store: Store) -> None:
    client = FakeClient()
    result = Loader(client, store).sync(D0, D0 + timedelta(days=13))  # 2 週間 = 10 営業日
    assert result.fetched["daily_bars"] == 10
    assert result.fetched["fin_summary"] == 10
    assert store.con.execute("SELECT count(*) FROM daily_bars").fetchone()[0] == 20
    assert result.requests == client.request_count


def test_sync_resumes_without_refetching(store: Store) -> None:
    Loader(FakeClient(), store).sync(D0, D0 + timedelta(days=13), limit_days=4)
    client = FakeClient()
    result = Loader(client, store).sync(D0, D0 + timedelta(days=13))
    bar_calls = [c for c in client.calls if c[0] == "bars"]
    assert len(bar_calls) == 6  # 取得済みの 4 日は飛ばす
    assert result.fetched["daily_bars"] == 6


def test_listed_info_monthly_snapshots(store: Store) -> None:
    client = FakeClient()
    Loader(client, store).sync(date(2024, 9, 2), date(2024, 10, 31), include_fin=False)
    snaps = [c[1] for c in client.calls if c[0] == "listed"]
    assert snaps == [date(2024, 9, 2), date(2024, 10, 1), date(2024, 10, 31)]


def test_out_of_plan_day_is_skipped_and_retried_later(store: Store) -> None:
    bad = D0 + timedelta(days=1)
    result = Loader(FakeClient(fail_on={bad}), store).sync(
        D0, D0 + timedelta(days=4), include_fin=False
    )
    assert result.fetched["daily_bars"] == 4
    assert len(result.errors) == 1
    assert bad not in store.completed_dates("daily_bars")
    # 次回は失敗した日だけ取りに行く
    client = FakeClient()
    Loader(client, store).sync(D0, D0 + timedelta(days=4), include_fin=False)
    assert [c[1] for c in client.calls if c[0] == "bars"] == [bad]


def test_auth_error_aborts(store: Store) -> None:
    class Unauthorized(FakeClient):
        def daily_bars(self, *a: object, **k: object) -> pd.DataFrame:
            resp = requests.Response()
            resp.status_code = 401
            raise HTTPError("401", response=resp)

    with pytest.raises(HTTPError):
        Loader(Unauthorized(), store).sync(D0, D0 + timedelta(days=4), include_fin=False)
    status = store.con.execute("SELECT status FROM fetch_log WHERE job = 'daily_bars'").fetchone()[
        0
    ]
    assert status == "error"


def test_split_triggers_full_refetch_only_when_requested(store: Store) -> None:
    split_day = D0 + timedelta(days=2)
    client = FakeClient(split=split_day)
    Loader(client, store).sync(D0, D0 + timedelta(days=4), include_fin=False)
    assert not [c for c in client.calls if c[0] == "bars" and c[1] == "72030"]

    store2 = Store(":memory:")
    client2 = FakeClient(split=split_day)
    Loader(client2, store2).sync(
        D0, D0 + timedelta(days=4), include_fin=False, refresh_splits=True, history_start=D0
    )
    assert [c[1] for c in client2.calls if c[0] == "bars" and isinstance(c[1], str)] == ["72030"]
    store2.close()
