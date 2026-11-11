"""手法ライブラリのユニットテスト。

実際のDBは使わず、最小限の DataFrame を組み立てて動作を確認する。
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from core.strategies import (
    BuffettStrategy,
    GrahamStrategy,
    GreenblattStrategy,
    HighDividendStrategy,
    LynchStrategy,
    MomentumStrategy,
    STRATEGY_CLASSES,
    TseReformStrategy,
)
from core.strategies.high_dividend import _count_no_cut_years


# --- テスト用フィクスチャ ---------------------------------------------------

def _make_fin(overrides: dict | None = None) -> pd.DataFrame:
    """最小限の fin_summary 行を1銘柄分作る。"""
    base = {
        "code": "72030",
        "disclosed_date": date(2025, 5, 1),
        "net_profit": 3_000_000_000,   # 30億円
        "equity": 20_000_000_000,       # 200億円
        "total_assets": 50_000_000_000, # 500億円
        "operating_profit": 4_000_000_000,
        "eps": Decimal("500"),
        "f_eps": Decimal("600"),
        "bps": Decimal("2000"),
        "div_annual": Decimal("100"),
        "payout_ratio": Decimal("0.20"),
        "shares_outstanding": 10_000_000,
        "cash_eq": 5_000_000_000,
    }
    if overrides:
        base.update(overrides)
    return pd.DataFrame([base])


def _make_prices(code: str = "72030", close: float = 1800.0) -> pd.DataFrame:
    return pd.DataFrame([{"code": code, "close": close, "name": "テスト株式"}])


AS_OF = date(2025, 6, 1)


# --- Strategy.describe() ---------------------------------------------------

@pytest.mark.parametrize("cls", STRATEGY_CLASSES.values())
def test_describe_returns_string(cls):
    assert isinstance(cls.describe(), str)
    assert len(cls.describe()) > 50


# --- BuffettStrategy -------------------------------------------------------

class TestBuffett:
    def test_pass_high_roe(self):
        # ROE = 30億 / 200億 = 15% → ギリギリ通過
        fin = _make_fin()
        prices = _make_prices(close=1800.0)
        results = BuffettStrategy(min_roe=0.15).screen(fin, prices, AS_OF)
        assert len(results) == 1
        assert results[0].code == "72030"

    def test_reject_low_roe(self):
        fin = _make_fin({"net_profit": 100_000_000})  # ROE < 1%
        prices = _make_prices()
        results = BuffettStrategy(min_roe=0.15).screen(fin, prices, AS_OF)
        assert len(results) == 0

    def test_reject_high_per(self):
        # PER = 1800 / 500 = 3.6（max_per=3 なら通らない）
        fin = _make_fin()
        prices = _make_prices(close=1800.0)
        results = BuffettStrategy(min_roe=0.10, max_per=3.0).screen(fin, prices, AS_OF)
        assert len(results) == 0

    def test_score_is_roe(self):
        fin = _make_fin()
        prices = _make_prices()
        results = BuffettStrategy(min_roe=0.10).screen(fin, prices, AS_OF)
        assert abs(results[0].score - 0.15) < 1e-6

    def test_metrics_keys(self):
        fin = _make_fin()
        prices = _make_prices()
        results = BuffettStrategy(min_roe=0.10).screen(fin, prices, AS_OF)
        assert "ROE" in results[0].metrics
        assert "PER" in results[0].metrics


# --- GrahamStrategy --------------------------------------------------------

class TestGraham:
    def test_pass_when_ncav_positive(self):
        # NCAV近似 = equity - (total_assets - equity) = 200億 - 300億 = -100億 → 負なので弾かれる
        fin = _make_fin()
        prices = _make_prices()
        results = GrahamStrategy().screen(fin, prices, AS_OF)
        assert len(results) == 0  # NCAV < 0 で弾かれる

    def test_pass_net_cash_rich(self):
        # NCAV = 12e9 - 3e9 = 9e9、市場 = 1800 * 1e7 = 18e9 → ratio = 2.0
        fin = _make_fin({"total_assets": 15_000_000_000, "equity": 12_000_000_000})
        prices = _make_prices(close=1800.0)
        results = GrahamStrategy(max_ncav_ratio=100).screen(fin, prices, AS_OF)
        assert len(results) == 1
        assert results[0].metrics["時価総額/NCAV"] == pytest.approx(2.0, rel=0.01)
        results2 = GrahamStrategy(max_ncav_ratio=1.0).screen(fin, prices, AS_OF)
        assert len(results2) == 0

    def test_pass_very_low_price(self):
        # 株価を極端に下げて時価総額 < NCAV にする
        fin = _make_fin({"total_assets": 15_000_000_000, "equity": 12_000_000_000})
        # NCAV = 9e9、市場 = 100 * 1e7 = 1e9 → ratio = 0.11 < 1.0
        prices = _make_prices(close=100.0)
        results = GrahamStrategy(max_ncav_ratio=1.0).screen(fin, prices, AS_OF)
        assert len(results) == 1
        assert results[0].metrics["時価総額/NCAV"] < 1.0


# --- GreenblattStrategy ----------------------------------------------------

class TestGreenblatt:
    def test_top_n_respected(self):
        rows = []
        for i in range(10):
            rows.append({
                "code": f"1{i:04d}0",
                "disclosed_date": date(2025, 5, 1),
                "operating_profit": (i + 1) * 1_000_000_000,
                "total_assets": 50_000_000_000,
                "equity": 20_000_000_000,
                "shares_outstanding": 10_000_000,
                "eps": None, "f_eps": None, "bps": None,
                "div_annual": None, "payout_ratio": None, "cash_eq": None,
                "net_profit": None,
            })
        fin = pd.DataFrame(rows)
        prices = pd.DataFrame([
            {"code": f"1{i:04d}0", "close": 2000.0, "name": f"株{i}"} for i in range(10)
        ])
        results = GreenblattStrategy(top_n=3, min_market_cap=0).screen(fin, prices, AS_OF)
        assert len(results) == 3

    def test_market_cap_filter(self):
        fin = _make_fin()
        # 時価総額 = 1800 * 1000万 = 180億 → 500億下限なら弾かれる
        prices = _make_prices(close=1800.0)
        results = GreenblattStrategy(min_market_cap=50_000_000_000).screen(fin, prices, AS_OF)
        assert len(results) == 0


# --- LynchStrategy ---------------------------------------------------------

class TestLynch:
    def test_pass_low_peg(self):
        # EPS=500, f_EPS=600 → growth=20%, PER=1800/500=3.6, PEG=3.6/20=0.18
        fin = _make_fin()
        prices = _make_prices(close=1800.0)
        results = LynchStrategy(max_peg=1.0, min_eps_growth=0.05).screen(fin, prices, AS_OF)
        assert len(results) == 1
        assert results[0].metrics["PEG"] == pytest.approx(0.18, rel=0.01)

    def test_reject_high_peg(self):
        # EPS=500, f_EPS=510 → growth=2%, PER=1800/500=3.6, PEG=3.6/2=1.8
        fin = _make_fin({"eps": Decimal("500"), "f_eps": Decimal("510")})
        prices = _make_prices(close=1800.0)
        results = LynchStrategy(max_peg=1.0, min_eps_growth=0.01).screen(fin, prices, AS_OF)
        assert len(results) == 0

    def test_reject_negative_eps(self):
        fin = _make_fin({"eps": Decimal("-100")})
        prices = _make_prices()
        results = LynchStrategy().screen(fin, prices, AS_OF)
        assert len(results) == 0


# --- HighDividendStrategy --------------------------------------------------

class TestHighDividend:
    def test_pass_high_yield(self):
        # 配当100円 / 株価1800円 = 5.6%
        fin = _make_fin()
        prices = _make_prices(close=1800.0)
        results = HighDividendStrategy(min_yield=0.03).screen(fin, prices, AS_OF)
        assert len(results) == 1
        assert results[0].metrics["配当利回り"] == pytest.approx(100 / 1800, rel=0.01)

    def test_reject_low_yield(self):
        # 配当100円 / 株価10000円 = 1%
        fin = _make_fin()
        prices = _make_prices(close=10000.0)
        results = HighDividendStrategy(min_yield=0.03).screen(fin, prices, AS_OF)
        assert len(results) == 0

    def test_reject_high_payout(self):
        fin = _make_fin({"payout_ratio": Decimal("0.90")})
        prices = _make_prices(close=1800.0)
        results = HighDividendStrategy(min_yield=0.03, max_payout_ratio=0.80).screen(fin, prices, AS_OF)
        assert len(results) == 0


class TestCountNocut:
    def test_all_increasing(self):
        s = pd.Series([50.0, 60.0, 70.0, 80.0])
        assert _count_no_cut_years(s) == 3

    def test_cut_in_middle(self):
        s = pd.Series([50.0, 40.0, 70.0, 80.0])
        assert _count_no_cut_years(s) == 2

    def test_single(self):
        assert _count_no_cut_years(pd.Series([100.0])) == 0

    def test_empty(self):
        assert _count_no_cut_years(pd.Series([], dtype=float)) == 0


# --- TseReformStrategy -----------------------------------------------------

class TestTseReform:
    def test_pass_low_pbr(self):
        # BPS=2000, 株価=1800 → PBR=0.9
        fin = _make_fin()
        prices = _make_prices(close=1800.0)
        results = TseReformStrategy(max_pbr=1.0).screen(fin, prices, AS_OF)
        assert len(results) == 1
        assert results[0].metrics["PBR"] == pytest.approx(0.9, rel=0.01)

    def test_reject_high_pbr(self):
        # BPS=2000, 株価=3000 → PBR=1.5
        fin = _make_fin()
        prices = _make_prices(close=3000.0)
        results = TseReformStrategy(max_pbr=1.0).screen(fin, prices, AS_OF)
        assert len(results) == 0


# --- MomentumStrategy ------------------------------------------------------

class TestMomentum:
    def test_screen_returns_empty_without_history(self):
        fin = _make_fin()
        prices = _make_prices()
        results = MomentumStrategy().screen(fin, prices, AS_OF)
        assert results == []

    def test_screen_with_history(self):
        # 3銘柄の価格履歴を組み立てる
        import numpy as np
        dates = pd.date_range("2024-06-01", periods=300, freq="B").date.tolist()
        rows = []
        for code, start in [("11110", 1000), ("22220", 2000), ("33330", 500)]:
            for i, d in enumerate(dates):
                rows.append({"code": code, "date": d, "close": float(start + i)})
        hist = pd.DataFrame(rows)
        listed = pd.DataFrame([
            {"code": "11110", "name": "A"},
            {"code": "22220", "name": "B"},
            {"code": "33330", "name": "C"},
        ])
        results = MomentumStrategy(top_n=2, min_market_cap=0).screen_with_history(
            hist, listed, dates[-1]
        )
        assert 1 <= len(results) <= 2
        # スコアは降順
        if len(results) > 1:
            assert results[0].score >= results[1].score
