"""core/paper/portfolio のユニットテスト。"""

from __future__ import annotations

import datetime
import tempfile
from pathlib import Path

import pytest

from core.paper.portfolio import PaperPortfolio


@pytest.fixture
def portfolio(tmp_path: Path) -> PaperPortfolio:
    return PaperPortfolio(db_path=tmp_path / "test.duckdb")


@pytest.fixture
def acct(portfolio: PaperPortfolio):
    return portfolio.get_or_create_account(initial_cash=1_000_000)


def test_create_account(portfolio: PaperPortfolio) -> None:
    acct = portfolio.get_or_create_account(initial_cash=5_000_000)
    assert acct.cash == 5_000_000
    assert acct.initial_cash == 5_000_000


def test_buy_basic(portfolio: PaperPortfolio, acct) -> None:
    trade = portfolio.buy(
        code="72030",
        shares=100,
        price=2500.0,
        reason="バフェット基準を満たす高ROE銘柄",
    )
    assert trade.side == "buy"
    assert trade.shares == 100
    assert trade.amount == 250_000

    acct_after = portfolio.get_account()
    assert acct_after.cash == 1_000_000 - 250_000


def test_buy_requires_reason(portfolio: PaperPortfolio, acct) -> None:
    with pytest.raises(ValueError, match="投資理由"):
        portfolio.buy(code="72030", shares=100, price=2500.0, reason="")


def test_buy_insufficient_cash(portfolio: PaperPortfolio, acct) -> None:
    with pytest.raises(ValueError, match="資金不足"):
        portfolio.buy(code="72030", shares=10000, price=2500.0, reason="テスト")


def test_sell_basic(portfolio: PaperPortfolio, acct) -> None:
    portfolio.buy(code="72030", shares=200, price=2000.0, reason="テスト買い")
    trade = portfolio.sell(code="72030", shares=100, price=2500.0, reason="利確テスト")
    assert trade.side == "sell"
    assert trade.shares == 100

    positions = portfolio.get_positions()
    assert len(positions) == 1
    assert positions[0].shares == 100


def test_sell_all_removes_position(portfolio: PaperPortfolio, acct) -> None:
    portfolio.buy(code="72030", shares=100, price=2000.0, reason="テスト")
    portfolio.sell(code="72030", shares=100, price=2500.0, reason="全売却テスト")
    positions = portfolio.get_positions()
    assert len(positions) == 0


def test_sell_more_than_held_raises(portfolio: PaperPortfolio, acct) -> None:
    portfolio.buy(code="72030", shares=100, price=2000.0, reason="テスト")
    with pytest.raises(ValueError, match="保有株数が不足"):
        portfolio.sell(code="72030", shares=200, price=2500.0, reason="超過売却テスト")


def test_average_cost(portfolio: PaperPortfolio, acct) -> None:
    portfolio.buy(code="72030", shares=100, price=2000.0, reason="1回目")
    portfolio.buy(code="72030", shares=100, price=3000.0, reason="2回目")
    positions = portfolio.get_positions()
    assert len(positions) == 1
    assert positions[0].shares == 200
    assert positions[0].avg_cost == pytest.approx(2500.0)


def test_get_trades(portfolio: PaperPortfolio, acct) -> None:
    portfolio.buy(code="72030", shares=100, price=2000.0, reason="テスト1")
    portfolio.buy(code="79740", shares=50, price=5000.0, reason="テスト2")
    df = portfolio.get_trades()
    assert len(df) == 2


def test_save_and_get_nav(portfolio: PaperPortfolio, acct) -> None:
    portfolio.buy(code="72030", shares=100, price=2000.0, reason="テスト")
    positions = portfolio.get_positions()
    for p in positions:
        p.current_price = 2500.0
    acct_now = portfolio.get_account()
    portfolio.save_nav(datetime.date.today(), positions, acct_now.cash)

    nav_df = portfolio.get_nav_history()
    assert len(nav_df) == 1
    assert nav_df["nav"].iloc[0] == acct_now.cash + 100 * 2500


def test_reset_account(portfolio: PaperPortfolio, acct) -> None:
    portfolio.buy(code="72030", shares=100, price=2000.0, reason="テスト")
    portfolio.reset_account(initial_cash=2_000_000)
    acct_after = portfolio.get_account()
    assert acct_after.cash == 2_000_000
    assert portfolio.get_positions() == []
    assert portfolio.get_trades().empty
