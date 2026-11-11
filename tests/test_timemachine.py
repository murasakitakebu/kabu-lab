"""core.calc.timemachine のテスト（メモリ DB に架空データを入れて計算）。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from core.calc.returns import Options
from core.calc.timemachine import Request, compare, run, simulate_code
from core.data.store import Store

START = date(2024, 1, 1)
DAYS = [START + timedelta(days=i) for i in range(400) if (START + timedelta(days=i)).weekday() < 5]


def _bars(code: str, price: float, growth: float = 0.0, until: date | None = None) -> pd.DataFrame:
    rows = []
    for i, d in enumerate(DAYS):
        if until is not None and d > until:
            break
        p = round(price * (1 + growth) ** i, 1)
        rows.append({"Date": pd.Timestamp(d), "Code": code, "C": p, "AdjC": p, "AdjFactor": 1.0})
    return pd.DataFrame(rows)


@pytest.fixture
def store() -> Iterator[Store]:
    s = Store(":memory:")
    s.upsert_trading_calendar(
        pd.DataFrame(
            {
                "Date": pd.to_datetime([START + timedelta(days=i) for i in range(400)]),
                "HolDiv": [
                    "1" if (START + timedelta(days=i)).weekday() < 5 else "0" for i in range(400)
                ],
            }
        )
    )
    s.upsert_daily_bars(_bars("72030", 1000.0))
    s.upsert_daily_bars(_bars("13060", 2000.0, growth=0.001))
    s.upsert_daily_bars(_bars("13210", 30000.0))
    s.upsert_daily_bars(_bars("99990", 500.0, until=date(2024, 6, 28)))  # 途中で上場廃止
    s.upsert_listed_info(
        pd.DataFrame(
            [
                {"Code": "72030", "CoName": "トヨタ自動車", "MktNm": "プライム"},
                {"Code": "99990", "CoName": "廃止テスト", "MktNm": "スタンダード"},
            ]
        ),
        as_of=START,
    )
    # 2023 年度（2023/4〜2024/3）の期末配当 50 円を 2024/5/8 に開示
    raw = {"CurFYSt": "2023-04-01", "CurFYEn": "2024-03-31", "DivFY": "50.0", "Code": "72030"}
    s.upsert_fin_summary(
        pd.DataFrame(
            [
                {
                    "DiscNo": "1",
                    "Code": "72030",
                    "DiscDate": pd.Timestamp("2024-05-08"),
                    "CurFYSt": "2023-04-01",
                    "CurFYEn": "2024-03-31",
                    "DivFY": json.loads(json.dumps(raw))["DivFY"],
                }
            ]
        )
    )
    yield s
    s.close()


def test_lump_sum_with_estimated_dividend(store: Store) -> None:
    req = Request(
        code="7203",
        start=START,
        amount=100_000,
        end=date(2024, 12, 31),
        opts=Options(reinvest_dividends=False, nisa=True),
    )
    r = simulate_code(store, req.code, req)
    assert r.shares == 100
    # 基準日 3/31(日) → 権利落ち 3/28。100 株 × 50 円 = 5,000 円（NISA）
    assert r.dividends_gross == 5_000
    assert r.total_value == 105_000
    assert any("推定" in n for n in r.notes)


def test_run_includes_benchmarks_and_deposit(store: Store) -> None:
    res = run(store, Request(code="7203", start=START, amount=100_000, end=date(2024, 12, 31)))
    assert set(res.benchmarks) == {"TOPIX（1306）", "日経平均（1321）"}
    assert res.benchmarks["TOPIX（1306）"].total_value > 100_000  # 右肩上がり
    # 横ばい（端数株は小数 6 桁で切り捨てるので 1 円未満の誤差が出る）
    assert res.benchmarks["日経平均（1321）"].total_value == pytest.approx(100_000, abs=1)
    assert set(res.chart["series"]) >= {"この銘柄", "投資額（元本）", "定期預金（年0.20%）"}
    assert any("ETF" in n for n in res.notes)


def test_dca_mode(store: Store) -> None:
    req = Request(
        code="7203", start=START, amount=10_000, end=date(2024, 3, 31), mode="dca", monthly_day=1
    )
    res = run(store, req)
    assert res.main.invested == 30_000  # 1/1(1/2 に執行), 2/1, 3/1
    assert res.deposit["invested"].iloc[-1] == 30_000


def test_delisted_stock_is_noted(store: Store) -> None:
    res = run(store, Request(code="9999", start=START, amount=100_000, end=date(2024, 12, 31)))
    assert res.main.end == date(2024, 6, 28)
    assert any("上場廃止" in n for n in res.notes)


def test_unknown_code_raises(store: Store) -> None:
    with pytest.raises(ValueError, match="株価データがありません"):
        run(store, Request(code="1111", start=START, amount=100_000))


def test_compare_skips_missing(store: Store) -> None:
    req = Request(code="7203", start=START, amount=100_000, end=date(2024, 12, 31))
    out = compare(store, ["7203", "1111", "9999"], req)
    assert list(out) == ["72030", "99990"]
    with pytest.raises(ValueError):
        compare(store, ["1"] * 6, req)


def test_search_securities(store: Store) -> None:
    assert store.search_securities("7203")["code"].tolist() == ["72030"]
    assert store.search_securities("トヨタ")["code"].tolist() == ["72030"]
    assert store.search_securities("廃止")["code"].tolist() == ["99990"]
    assert store.search_securities("").empty


def test_dividend_amount_is_decimal(store: Store) -> None:
    fin = store.fin_summary_with_raw("7203")
    assert json.loads(fin.iloc[0]["raw"])["DivFY"] == "50.0"
    assert Decimal(json.loads(fin.iloc[0]["raw"])["DivFY"]) == Decimal("50.0")
