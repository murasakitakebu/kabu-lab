"""バックテストエンジン。

定期リバランス戦略の過去検証を行う。
ルックアヘッドバイアス防止・生存バイアス対応に関する注意を出力に含める。

設計上の前提:
- 銘柄選択は Strategy.screen() / screen_with_history() に委譲する
- リバランスは月次・四半期・年次のいずれか
- 加重方法は等ウェイトのみ（Phase 2 スコープ）
- 取引コストはデフォルト 0 円（パラメータで変更可）
- 約定は「スクリーニング翌営業日の始値」。DB に始値がない場合は終値で代替
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

import pandas as pd

from core.strategies.base import Strategy

Frequency = Literal["monthly", "quarterly", "yearly"]


@dataclass(frozen=True)
class BacktestConfig:
    """バックテスト設定。"""

    start: date
    end: date
    initial_cash: float = 10_000_000.0
    rebalance_freq: Frequency = "quarterly"
    max_holdings: int = 30
    commission_rate: float = 0.0
    min_market_cap: float = 0.0


@dataclass
class DailyRecord:
    """日次の資産記録。"""

    date: date
    portfolio_value: float
    cash: float
    n_holdings: int


@dataclass
class Trade:
    """1 件の取引記録。"""

    date: date
    code: str
    name: str
    action: Literal["buy", "sell"]
    shares: float
    price: float
    commission: float


@dataclass
class BacktestResult:
    """バックテスト結果。"""

    config: BacktestConfig
    daily: pd.DataFrame
    trades: pd.DataFrame
    holdings_history: pd.DataFrame
    warnings: list[str]

    cagr: float | None = None
    max_drawdown: float | None = None
    sharpe: float | None = None
    total_return: float | None = None
    annual_returns: pd.Series | None = None


def _rebalance_dates(start: date, end: date, freq: Frequency) -> list[date]:
    """リバランス日のリストを生成する。"""
    dates = []
    cur = start
    while cur <= end:
        dates.append(cur)
        if freq == "monthly":
            if cur.month == 12:
                cur = date(cur.year + 1, 1, 1)
            else:
                cur = date(cur.year, cur.month + 1, 1)
        elif freq == "quarterly":
            m = cur.month + 3
            y = cur.year + (m - 1) // 12
            m = (m - 1) % 12 + 1
            cur = date(y, m, 1)
        else:
            cur = date(cur.year + 1, cur.month, 1)
    return dates


def _next_trading_day(d: date, trading_days: set[date]) -> date | None:
    """d 以降の最初の取引日を返す（最大 10 日先まで）。"""
    for i in range(10):
        candidate = d + timedelta(days=i)
        if candidate in trading_days:
            return candidate
    return None


def rebalance_dates_on_market(rebal_dates: list[date], trading_days: set[date]) -> list[date]:
    """リバランス日を最寄りの取引日に調整する。"""
    result = []
    for d in rebal_dates:
        td = _next_trading_day(d, trading_days)
        if td:
            result.append(td)
    return result


def run(
    strategy: Strategy,
    config: BacktestConfig,
    prices_all: pd.DataFrame,
    fin_all: pd.DataFrame,
    listed_all: pd.DataFrame,
) -> BacktestResult:
    """バックテストを実行する。

    Args:
        strategy: screen() または screen_with_history() を持つ Strategy インスタンス
        config: バックテスト設定
        prices_all: 全銘柄・全期間の日足。列 code, date, open, adj_close。
        fin_all: 全銘柄・全期間の財務サマリ。disclosed_date を含む。
        listed_all: 全期間の上場銘柄一覧。列 code, name, as_of。
    """
    from core.strategies.momentum import MomentumStrategy

    warnings: list[str] = [
        "⚠️ 生存バイアス：上場廃止銘柄が除外されている場合、実際の成績は過大評価される可能性があります。",
        "⚠️ ルックアヘッド：財務データは開示日ベースで使用していますが、スクリーニング精度は取得データの品質に依存します。",
        (
            "⚠️ 取引コスト："
            + (
                f"片道 {config.commission_rate * 100:.2f}% を計上しています。"
                if config.commission_rate > 0
                else "取引コスト 0 円を仮定しています（実際の成績は下回ります）。"
            )
        ),
    ]

    prices_all = prices_all.copy()
    prices_all["date"] = pd.to_datetime(prices_all["date"]).dt.date
    trading_days: set[date] = set(prices_all["date"].unique())

    price_map: dict[tuple[str, date], float] = {
        (r.code, r.date): r.adj_close
        for r in prices_all[["code", "date", "adj_close"]].dropna().itertuples(index=False)
    }
    open_map: dict[tuple[str, date], float] = {}
    if "open" in prices_all.columns:
        open_map = {
            (r.code, r.date): r.open
            for r in prices_all[["code", "date", "open"]].dropna().itertuples(index=False)
        }

    rebal_dates = _rebalance_dates(config.start, config.end, config.rebalance_freq)
    all_dates = sorted(d for d in trading_days if config.start <= d <= config.end)
    rebal_set = set(rebalance_dates_on_market(rebal_dates, trading_days))

    cash = config.initial_cash
    holdings: dict[str, float] = {}
    daily_records: list[DailyRecord] = []
    trades_list: list[Trade] = []
    holdings_history_rows: list[dict] = []

    def portfolio_value(d: date) -> float:
        v = cash
        for code, shares in holdings.items():
            p = price_map.get((code, d))
            if p:
                v += shares * p
        return v

    for d in all_dates:
        if d in rebal_set:
            screen_date = d - timedelta(days=1)

            fin_snap = fin_all[fin_all["disclosed_date"] <= screen_date]
            prices_snap = (
                prices_all[prices_all["date"] <= screen_date]
                .sort_values("date")
                .groupby("code")
                .last()
                .reset_index()[["code", "adj_close"]]
                .rename(columns={"adj_close": "close"})
            )
            listed_snap = (
                listed_all[listed_all["as_of"] <= screen_date]
                .sort_values("as_of")
                .groupby("code")
                .last()
                .reset_index()[["code", "name"]]
            )
            prices_snap = prices_snap.merge(listed_snap, on="code", how="left")

            if isinstance(strategy, MomentumStrategy):
                hist = (
                    prices_all[prices_all["date"] <= screen_date]
                    .rename(columns={"adj_close": "close"})
                )
                screen_results = strategy.screen_with_history(hist, listed_snap, screen_date)
            else:
                screen_results = strategy.screen(fin_snap, prices_snap, screen_date)

            new_codes = {r.code for r in screen_results[: config.max_holdings]}
            name_map = dict(zip(listed_snap["code"], listed_snap["name"]))

            for code in list(holdings.keys()):
                if code not in new_codes:
                    exec_price = open_map.get((code, d)) or price_map.get((code, d))
                    if exec_price and holdings[code] > 0:
                        proceeds = holdings[code] * exec_price
                        commission = proceeds * config.commission_rate
                        cash += proceeds - commission
                        trades_list.append(Trade(
                            d, code, name_map.get(code, code), "sell",
                            holdings[code], exec_price, commission,
                        ))
                    del holdings[code]

            buy_codes = [c for c in new_codes if c not in holdings]
            n_total = len(new_codes)
            if n_total > 0:
                target_value = portfolio_value(d) / n_total
                for code in buy_codes:
                    exec_price = open_map.get((code, d)) or price_map.get((code, d))
                    if not exec_price or exec_price <= 0:
                        continue
                    shares = (target_value * (1 - config.commission_rate)) / exec_price
                    cost = shares * exec_price
                    commission = cost * config.commission_rate
                    if cash >= cost + commission:
                        cash -= cost + commission
                        holdings[code] = holdings.get(code, 0) + shares
                        trades_list.append(Trade(
                            d, code, name_map.get(code, code), "buy",
                            shares, exec_price, commission,
                        ))

            for code, shares in holdings.items():
                holdings_history_rows.append({
                    "rebalance_date": d,
                    "code": code,
                    "name": name_map.get(code, code),
                    "shares": shares,
                })

        pv = portfolio_value(d)
        daily_records.append(DailyRecord(d, pv, cash, len(holdings)))

    daily_df = pd.DataFrame([vars(r) for r in daily_records])
    trades_df = pd.DataFrame([vars(t) for t in trades_list]) if trades_list else pd.DataFrame()
    holdings_df = pd.DataFrame(holdings_history_rows) if holdings_history_rows else pd.DataFrame()

    result = BacktestResult(
        config=config,
        daily=daily_df,
        trades=trades_df,
        holdings_history=holdings_df,
        warnings=warnings,
    )
    _compute_metrics(result)
    return result


def _compute_metrics(result: BacktestResult) -> None:
    """BacktestResult にパフォーマンス指標をセットする（インプレース）。"""
    if result.daily.empty:
        return

    daily = result.daily.copy()
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.set_index("date").sort_index()

    pv = daily["portfolio_value"]
    if pv.empty or pv.iloc[0] == 0:
        return

    result.total_return = pv.iloc[-1] / result.config.initial_cash - 1

    n_years = (daily.index[-1] - daily.index[0]).days / 365.25
    if n_years > 0:
        result.cagr = (pv.iloc[-1] / result.config.initial_cash) ** (1 / n_years) - 1

    peak = pv.cummax()
    drawdown = (pv - peak) / peak
    result.max_drawdown = float(drawdown.min())

    daily_ret = pv.pct_change().dropna()
    if daily_ret.std() > 0:
        result.sharpe = float(daily_ret.mean() / daily_ret.std() * (252 ** 0.5))

    annual = pv.resample("YE").last().pct_change().dropna()
    result.annual_returns = annual
