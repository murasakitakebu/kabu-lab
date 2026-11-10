"""core.data.store のテスト（メモリ DB を使い、ファイルは作らない）。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from core.data.store import (
    TOPIX_CODE,
    Store,
    to_daily_bars,
    to_dividends,
    to_fin_summary,
)


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store(":memory:")
    yield s
    s.close()


def _bars(rows: list[tuple[str, str, float, float]]) -> pd.DataFrame:
    """(Code, Date, C, AdjC) から V2 形式の日足 DataFrame を作る。"""
    return pd.DataFrame(
        [
            {
                "Date": pd.Timestamp(d),
                "Code": code,
                "O": c,
                "H": c,
                "L": c,
                "C": c,
                "UL": "0",
                "LL": "0",
                "Vo": 1000.0,
                "Va": c * 1000,
                "AdjFactor": 1.0,
                "AdjO": adj,
                "AdjH": adj,
                "AdjL": adj,
                "AdjC": adj,
                "AdjVo": 1000.0,
            }
            for code, d, c, adj in rows
        ]
    )


def _fin(disc_no: str, code: str, disc_date: str, **over: object) -> dict[str, object]:
    row: dict[str, object] = {
        "DiscDate": pd.Timestamp(disc_date),
        "DiscTime": "15:00:00",
        "Code": code,
        "DiscNo": disc_no,
        "DocType": "FYFinancialStatements_Consolidated_IFRS",
        "CurPerType": "FY",
        "CurPerSt": "2023-04-01",
        "CurPerEn": "2024-03-31",
        "CurFYSt": "2023-04-01",
        "CurFYEn": "2024-03-31",
        "Sales": "45095325000000",
        "OP": "5352934000000",
        "NP": "4944933000000",
        "EPS": "365.94",
        "BPS": "2346.69",
        "DivAnn": "75.0",
        "FDivAnn": "",
        "EqAR": "0.382",
    }
    row.update(over)
    return row


# --- 変換 -------------------------------------------------------------------


def test_to_daily_bars_types_and_code() -> None:
    out = to_daily_bars(_bars([("7203", "2024-01-04", 2500.0, 2500.0)]))
    row = out.iloc[0]
    assert row["code"] == "72030"
    assert row["date"] == date(2024, 1, 4)
    assert row["adj_close"] == 2500.0


def test_to_fin_summary_parses_strings_and_missing() -> None:
    out = to_fin_summary(pd.DataFrame([_fin("1", "72030", "2024-05-08", FDivAnn="-")]))
    row = out.iloc[0]
    assert row["sales"] == 45_095_325_000_000
    assert isinstance(row["sales"], int)
    assert row["eps"] == Decimal("365.94")
    assert row["f_div_annual"] is None  # "-" は値なし
    assert row["ordinary_profit"] is None  # 列が無ければ None（IFRS は経常利益なし）
    assert row["period_end"] == date(2024, 3, 31)
    assert json.loads(row["raw"])["DiscNo"] == "1"


def test_to_dividends_handles_undecided_rate() -> None:
    df = pd.DataFrame(
        [
            {
                "PubDate": pd.Timestamp("2024-05-08"),
                "Code": "72030",
                "RefNo": "R1",
                "DivRate": "-",
                "RecDate": "2024-09-30",
                "ExDate": "",
            }
        ]
    )
    row = to_dividends(df).iloc[0]
    assert row["div_rate"] is None
    assert row["record_date"] == date(2024, 9, 30)
    assert row["ex_date"] is None


# --- upsert -----------------------------------------------------------------


def test_upsert_daily_bars_is_idempotent_and_updates(store: Store) -> None:
    df = _bars([("72030", "2024-01-04", 2500.0, 2500.0), ("72030", "2024-01-05", 2550.0, 2550.0)])
    assert store.upsert_daily_bars(df) == 2
    assert store.upsert_daily_bars(df) == 2  # 再実行しても重複しない
    # 分割で調整後価格が書き換わったケース：同じキーで上書きされる
    store.upsert_daily_bars(_bars([("72030", "2024-01-04", 2500.0, 500.0)]))
    got = store.daily_bars("7203")
    assert len(got) == 2
    assert got.iloc[0]["adj_close"] == 500.0
    assert got.iloc[0]["close"] == 2500.0  # 未調整値も保持


def test_upsert_dedups_within_batch(store: Store) -> None:
    df = _bars([("72030", "2024-01-04", 1.0, 1.0), ("72030", "2024-01-04", 2.0, 2.0)])
    assert store.upsert_daily_bars(df) == 1
    assert store.daily_bars("72030").iloc[0]["close"] == 2.0  # 後勝ち


def test_upsert_empty_is_noop(store: Store) -> None:
    assert store.upsert_daily_bars(pd.DataFrame()) == 0


def test_upsert_rejects_missing_key(store: Store) -> None:
    df = _bars([("72030", "2024-01-04", 1.0, 1.0)])
    df["Date"] = pd.NaT
    with pytest.raises(ValueError, match="主キー"):
        store.upsert_daily_bars(df)


def test_daily_bars_range_and_last_date(store: Store) -> None:
    store.upsert_daily_bars(
        _bars([("72030", f"2024-01-0{d}", float(d), float(d)) for d in range(4, 10)])
    )
    store.upsert_daily_bars(_bars([("79740", "2024-02-01", 1.0, 1.0)]))
    got = store.daily_bars("7203", start=date(2024, 1, 5), end=date(2024, 1, 8))
    assert got["date"].dt.date.tolist() == [date(2024, 1, d) for d in range(5, 9)]
    assert store.last_date("daily_bars", "7203") == date(2024, 1, 9)
    assert store.last_date("daily_bars") == date(2024, 2, 1)
    assert store.last_date("dividends") is None


def test_index_bars_topix(store: Store) -> None:
    df = pd.DataFrame(
        {"Date": pd.to_datetime(["2024-01-04"]), "O": [1], "H": [2], "L": [0.5], "C": [1.5]}
    )
    store.upsert_index_bars(df)
    got = store.index_bars(TOPIX_CODE)
    assert got.iloc[0]["close"] == 1.5


def test_fin_summary_known_at_prevents_lookahead(store: Store) -> None:
    store.upsert_fin_summary(
        pd.DataFrame(
            [
                _fin("1", "72030", "2024-02-06", CurPerType="3Q"),
                _fin("2", "72030", "2024-05-08"),
            ]
        )
    )
    # 期末日 3/31 時点では 5/8 開示の本決算はまだ見えない
    known = store.fin_summary_known_at("7203", date(2024, 3, 31))
    assert known["disc_no"].tolist() == ["1"]
    assert len(store.fin_summary_known_at("7203", date(2024, 5, 8))) == 2


def test_listed_info_snapshots_keep_delisted(store: Store) -> None:
    v1 = pd.DataFrame(
        [{"Code": "72030", "CoName": "トヨタ"}, {"Code": "99990", "CoName": "廃止社"}]
    )
    v2 = pd.DataFrame([{"Code": "72030", "CoName": "トヨタ自動車"}])
    store.upsert_listed_info(v1, date(2024, 1, 4))
    store.upsert_listed_info(v2, date(2024, 6, 3))
    latest = store.latest_listed_info().set_index("code")
    assert latest.loc["72030", "name"] == "トヨタ自動車"
    assert latest.loc["99990", "name"] == "廃止社"  # 上場廃止銘柄も残る
    assert latest.loc["99990", "as_of"].date() == date(2024, 1, 4)


def test_dividends_roundtrip(store: Store) -> None:
    df = pd.DataFrame(
        [
            {"PubDate": pd.Timestamp("2024-05-08"), "Code": "72030", "RefNo": "R2",
             "DivRate": "45", "ExDate": "2024-03-28"},
            {"PubDate": pd.Timestamp("2023-11-01"), "Code": "72030", "RefNo": "R1",
             "DivRate": "30", "ExDate": "2023-09-28"},
        ]
    )  # fmt: skip
    store.upsert_dividends(df)
    got = store.dividends("7203")
    assert got["ref_no"].tolist() == ["R1", "R2"]
    assert got.iloc[1]["div_rate"] == Decimal("45")


# --- 取得ログ ---------------------------------------------------------------


def test_fetch_job_logs_success(store: Store) -> None:
    with store.fetch_job("daily_bars", code="72030") as rec:
        rec.rows += store.upsert_daily_bars(_bars([("72030", "2024-01-04", 1.0, 1.0)]))
        rec.requests = 1
    row = store.con.execute(
        "SELECT job, status, rows, requests, params, error FROM fetch_log"
    ).fetchone()
    assert row[:4] == ("daily_bars", "ok", 1, 1)
    assert json.loads(row[4]) == {"code": "72030"}
    assert row[5] is None


def test_fetch_job_logs_error_and_reraises(store: Store) -> None:
    with pytest.raises(RuntimeError), store.fetch_job("topix"):
        raise RuntimeError("boom")
    status, error, finished = store.con.execute(
        "SELECT status, error, finished_at FROM fetch_log"
    ).fetchone()
    assert status == "error"
    assert error == "RuntimeError: boom"
    assert finished is not None


def test_schema_init_is_repeatable(store: Store) -> None:
    store.init_schema()
    assert store.con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == (
        "1",
    )
