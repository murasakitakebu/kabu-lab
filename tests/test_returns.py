"""core.calc（リターン計算・税金・配当推定）のテスト。期待値はすべて手計算。"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from core.calc.dividends import estimate_dividends, ex_date_for_record
from core.calc.returns import (
    Dividend,
    Options,
    Order,
    deposit,
    dollar_cost_averaging,
    lump_sum,
    monthly_orders,
    simulate,
    xirr,
)
from core.calc.tax import tax_on_income


def bars(
    prices: list[float], start: date = date(2024, 1, 1), factors: dict[int, float] | None = None
):
    """連続した日付の日足（index i の日に adj_factor を付けられる）。"""
    factors = factors or {}
    return pd.DataFrame(
        {
            "date": [start + timedelta(days=i) for i in range(len(prices))],
            "close": prices,
            "adj_factor": [factors.get(i, 1.0) for i in range(len(prices))],
        }
    )


D = [date(2024, 1, 1) + timedelta(days=i) for i in range(10)]


# --- 税金 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (10_000, 1_531 + 500),  # 1531.5 → 1531（切り捨て）
        (5_000, 765 + 250),  # 765.75 → 765
        (1, 0),
        (0, 0),
        (-5_000, 0),
    ],
)
def test_tax_on_income(amount: int, expected: int) -> None:
    assert tax_on_income(amount) == expected


def test_tax_nisa_is_zero() -> None:
    assert tax_on_income(1_000_000, nisa=True) == 0


# --- 一括投資の基本 ---------------------------------------------------------


def test_lump_sum_no_change() -> None:
    r = lump_sum(bars([1000, 1100, 1200]), D[0], 100_000)
    assert r.shares == 100
    assert r.total_value == 120_000
    assert r.profit == 20_000
    assert r.total_return == pytest.approx(0.2)
    assert r.after_tax_value == 120_000 - tax_on_income(20_000)


def test_lot_size_100_leaves_cash() -> None:
    r = lump_sum(bars([1000, 1000]), D[0], 150_000, opts=Options(lot_size=100))
    assert r.shares == 100
    assert r.cash == 50_000
    assert r.total_value == 150_000


def test_lot_size_too_small_buys_nothing() -> None:
    r = lump_sum(bars([3000, 3000]), D[0], 100_000, opts=Options(lot_size=100))
    assert r.shares == 0
    assert r.total_value == 100_000


def test_fractional_shares() -> None:
    r = lump_sum(bars([300, 600]), D[0], 1_000, opts=Options(lot_size=None))
    assert r.shares == Decimal("3.333333")
    assert r.total_value == 1_999  # 3.333333 × 600 = 1999.9998 → 切り捨て


def test_fee_is_part_of_cost_basis() -> None:
    r = lump_sum(bars([1000, 1100]), D[0], 100_000, opts=Options(fee_per_trade=500))
    assert r.shares == 99  # 99,500 円分しか買えない
    assert r.cost_basis == 99_000 + 500
    assert r.fees == 500
    # 売却: 99 × 1100 = 108,900 - 手数料 500 = 108,400、益 = 108,400 - 99,500 = 8,900
    assert r.after_tax_value == 108_400 - tax_on_income(8_900) + 500  # 残り現金 500


def test_buy_on_holiday_executes_next_trading_day() -> None:
    b = bars([1000, 1000, 2000]).drop(index=0)  # 1/1 は取引なし
    r = lump_sum(b, D[0], 100_000)
    assert r.start == D[1]
    assert r.shares == 100


def test_end_date_cuts_off() -> None:
    r = lump_sum(bars([1000, 1100, 5000]), D[0], 100_000, end=D[1])
    assert r.total_value == 110_000
    assert r.end == D[1]


# --- 分割・併合 -------------------------------------------------------------


def test_split_keeps_value() -> None:
    # 2 日目に 1:2 分割（AdjFactor 0.5）。未調整終値は半分になる
    r = lump_sum(bars([1000, 500, 550], factors={1: 0.5}), D[0], 100_000)
    assert r.shares == 200
    assert r.total_value == 110_000
    assert r.daily["value"].tolist() == [100_000, 100_000, 110_000]
    assert r.max_drawdown == 0  # 分割で下落に見えない


def test_reverse_split_cashes_out_fraction() -> None:
    # 3 株を 2:1 併合（AdjFactor 2）→ 1.5 株 → 1 株 + 0.5 株分を 2000 円で現金化
    r = lump_sum(bars([1000, 2000], factors={1: 2.0}), D[0], 3_000)
    assert r.shares == 1
    assert r.cash == 1_000
    assert r.total_value == 3_000


def test_dividend_after_split_uses_new_share_count() -> None:
    # 分割後に権利落ち → 分割後の株数で受け取る
    divs = [Dividend(D[2], D[3], Decimal("10"))]
    r = lump_sum(
        bars([1000, 500, 500, 500], factors={1: 0.5}),
        D[0],
        100_000,
        divs,
        Options(reinvest_dividends=False, nisa=True),
    )
    assert r.dividends_gross == 200 * 10


# --- 配当 -------------------------------------------------------------------


def test_dividend_without_reinvestment_is_taxed_cash() -> None:
    divs = [Dividend(ex_date=D[2], pay_date=D[4], amount_per_share=Decimal("50"))]
    r = lump_sum(bars([1000] * 6), D[0], 100_000, divs, Options(reinvest_dividends=False))
    assert r.dividends_gross == 5_000
    assert r.dividends_tax == 765 + 250
    assert r.shares == 100
    assert r.cash == 5_000 - 1_015
    assert r.total_value == 100_000 + 3_985


def test_dividend_receivable_counts_before_payment() -> None:
    divs = [Dividend(D[2], D[9], Decimal("50"))]
    r = lump_sum(bars([1000] * 4), D[0], 100_000, divs, Options(reinvest_dividends=False))
    assert r.dividends_receivable == 3_985
    assert r.total_value == 103_985
    assert any("未払い" in n for n in r.notes)


def test_dividend_reinvestment_buys_shares() -> None:
    divs = [Dividend(D[1], D[2], Decimal("50"))]
    opts = Options(reinvest_dividends=True, nisa=True)
    r = lump_sum(bars([100, 100, 100, 200]), D[0], 10_000, divs, opts)
    # 100 株 × 50 円 = 5,000 円（NISA なので非課税）→ 支払日に 100 円で 50 株
    assert r.shares == 150
    assert r.total_value == 150 * 200


def test_reinvest_vs_not_differs_when_price_rises() -> None:
    divs = [Dividend(D[1], D[2], Decimal("50"))]
    b = bars([100, 100, 100, 200])
    yes = lump_sum(b, D[0], 10_000, divs, Options(reinvest_dividends=True, nisa=True))
    no = lump_sum(b, D[0], 10_000, divs, Options(reinvest_dividends=False, nisa=True))
    assert yes.total_value == 30_000
    assert no.total_value == 25_000


def test_buying_on_ex_date_gets_no_dividend() -> None:
    divs = [Dividend(D[0], D[2], Decimal("50"))]
    r = lump_sum(bars([1000] * 3), D[0], 100_000, divs)
    assert r.dividends_gross == 0


def test_ex_date_on_non_trading_day_still_counts() -> None:
    b = bars([1000] * 5).drop(index=2)  # D[2] は休場
    divs = [Dividend(D[2], D[4], Decimal("10"))]
    r = lump_sum(b, D[0], 100_000, divs, Options(reinvest_dividends=False, nisa=True))
    assert r.dividends_gross == 1_000


# --- NISA -------------------------------------------------------------------


def test_nisa_no_tax_on_dividend_or_gain() -> None:
    divs = [Dividend(D[1], D[2], Decimal("50"))]
    opts = Options(reinvest_dividends=False, nisa=True)
    r = lump_sum(bars([1000, 1000, 1000, 1500]), D[0], 100_000, divs, opts)
    assert r.dividends_tax == 0
    assert r.total_value == 150_000 + 5_000
    assert r.after_tax_value == r.total_value


def test_taxable_vs_nisa_after_tax_gap() -> None:
    b = bars([1000, 2000])
    taxable = lump_sum(b, D[0], 100_000)
    nisa = lump_sum(b, D[0], 100_000, opts=Options(nisa=True))
    assert nisa.after_tax_value - taxable.after_tax_value == tax_on_income(100_000)


def test_loss_has_no_tax() -> None:
    r = lump_sum(bars([1000, 800]), D[0], 100_000)
    assert r.after_tax_value == 80_000


# --- 積立 -------------------------------------------------------------------


def test_monthly_orders_clamps_day_and_range() -> None:
    orders = monthly_orders(date(2024, 1, 15), date(2024, 4, 30), 10_000, day=31)
    assert [o.date for o in orders] == [
        date(2024, 1, 31),
        date(2024, 2, 29),
        date(2024, 3, 31),
        date(2024, 4, 30),
    ]


def test_dca_flat_price() -> None:
    b = bars([1000] * 100)  # 2024-01-01 〜 04-09
    r = dollar_cost_averaging(b, date(2024, 1, 1), date(2024, 4, 9), 10_000)
    assert r.invested == 40_000  # 1/1, 2/1, 3/1, 4/1
    assert r.shares == 40
    assert r.total_value == 40_000
    assert r.irr == pytest.approx(0.0, abs=1e-9)
    assert r.max_drawdown == 0


def test_dca_buys_more_when_cheap() -> None:
    prices = [1000] * 31 + [500] * 29 + [1000] * 10  # 2 月だけ半値
    r = dollar_cost_averaging(bars(prices), date(2024, 1, 1), date(2024, 3, 10), 10_000)
    assert r.shares == 10 + 20 + 10
    assert r.total_value == 40_000
    assert r.profit == 10_000


def test_dca_drawdown_ignores_contributions() -> None:
    prices = [1000] * 31 + [500] * 29 + [500] * 10
    r = dollar_cost_averaging(bars(prices), date(2024, 1, 1), date(2024, 3, 10), 10_000)
    # 拠出で評価額が増えても、下落率は価格の -50% で測る
    assert r.max_drawdown == pytest.approx(-0.5)


# --- ドローダウン・IRR ------------------------------------------------------


def test_max_drawdown_lump_sum() -> None:
    r = lump_sum(bars([100, 150, 75, 120]), D[0], 10_000)
    assert r.max_drawdown == pytest.approx(-0.5)


def test_irr_matches_cagr_for_lump_sum() -> None:
    b = bars([1000, 1210], start=date(2023, 1, 1))
    b.loc[1, "date"] = date(2025, 1, 1)  # 2 年で 1.21 倍 → 年率 10%（2024 はうるう年で 731 日）
    r = lump_sum(b, date(2023, 1, 1), 100_000)
    assert r.irr == pytest.approx(1.21 ** (365 / 731) - 1, rel=1e-6)


def test_xirr_simple() -> None:
    assert xirr([(date(2023, 1, 1), -100), (date(2024, 1, 1), 110)]) == pytest.approx(0.10)


def test_xirr_none_when_one_sided() -> None:
    assert xirr([(date(2023, 1, 1), -100)]) is None
    assert xirr([(date(2023, 1, 1), 100), (date(2024, 1, 1), 100)]) is None


def test_orders_after_end_are_noted() -> None:
    r = simulate(bars([1000, 1000]), [Order(D[0], 10_000), Order(D[5], 10_000)], end=D[1])
    assert r.invested == 10_000
    assert any("評価日以降" in n for n in r.notes)


def test_no_data_raises() -> None:
    with pytest.raises(ValueError):
        lump_sum(bars([1000]), date(2030, 1, 1), 10_000)


# --- 定期預金 ---------------------------------------------------------------


def test_deposit_one_year() -> None:
    df = deposit([Order(date(2023, 1, 1), 1_000_000)], [date(2024, 1, 1)], 0.01)
    row = df.iloc[0]
    assert row["value"] == 1_010_000
    assert row["after_tax_value"] == 1_010_000 - tax_on_income(10_000)


# --- 配当推定（Free プラン用） ---------------------------------------------


def _weekdays(start: date, end: date) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


CAL = _weekdays(date(2023, 1, 1), date(2025, 12, 31))


def test_ex_date_record_on_weekday() -> None:
    # 2024-09-30(月) が基準日 → 権利付最終日 9/26(木)、権利落ち日 9/27(金)
    assert ex_date_for_record(date(2024, 9, 30), CAL) == date(2024, 9, 27)


def test_ex_date_record_on_sunday() -> None:
    # 2024-03-31(日) → 受渡し最終は 3/29(金) → 権利付最終日 3/27(水)、権利落ち日 3/28(木)
    assert ex_date_for_record(date(2024, 3, 31), CAL) == date(2024, 3, 28)


def _fin_row(disclosed: str, **fields: str) -> dict[str, object]:
    raw = {"CurFYSt": "2023-04-01", "CurFYEn": "2024-03-31", **fields}
    return {"disclosed_date": pd.Timestamp(disclosed), "raw": json.dumps(raw)}


def test_estimate_dividends_interim_and_year_end() -> None:
    fin = pd.DataFrame(
        [
            _fin_row("2023-11-01", Div2Q="30.0", DivFY=""),  # 中間決算：中間配当の実績
            _fin_row("2024-05-08", Div2Q="30.0", DivFY="45.0"),  # 本決算
        ]
    )
    divs = estimate_dividends(fin, CAL)
    assert [(d.ex_date, d.amount_per_share) for d in divs] == [
        (date(2023, 9, 28), Decimal("30.0")),  # 9/30(土) → 受渡し 9/29 → 落ち日 9/28
        (date(2024, 3, 28), Decimal("45.0")),
    ]
    assert all(d.estimated for d in divs)
    assert divs[1].pay_date == date(2024, 6, 14)  # 3/31 + 75 日 = 6/14(金)


def test_estimate_prefers_earliest_actual() -> None:
    # 後の短信で分割修正された値（15.0）より、基準日直後の値（30.0）を採る
    fin = pd.DataFrame([_fin_row("2024-05-08", Div2Q="15.0"), _fin_row("2023-11-01", Div2Q="30.0")])
    assert estimate_dividends(fin, CAL)[0].amount_per_share == Decimal("30.0")


def test_estimate_ignores_blank_zero_and_pre_record() -> None:
    fin = pd.DataFrame(
        [
            _fin_row("2023-08-01", Div2Q="30.0"),  # 基準日前の開示（＝実績ではない）
            _fin_row("2024-05-08", Div1Q="-", Div2Q="0.0", DivFY=""),
        ]
    )
    assert estimate_dividends(fin, CAL) == []


def test_estimated_dividend_is_noted_in_result() -> None:
    divs = [Dividend(D[1], D[2], Decimal("10"), estimated=True)]
    r = lump_sum(bars([1000] * 3), D[0], 100_000, divs)
    assert any("推定" in n for n in r.notes)
