"""グリーンブラット（魔法の公式）。

益回り（EBIT/EV）と資本収益率（EBIT/投下資本）の合成順位で銘柄を選ぶ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from core.strategies.base import ReportFocus, ScreenResult, Strategy, StrategyMeta


@dataclass
class GreenblattStrategy(Strategy):
    """グリーンブラット 魔法の公式。

    Args:
        top_n: 合成順位の上位何銘柄を返すか（None なら全件）
        min_market_cap: 時価総額の下限（円）
    """

    top_n: int | None = 30
    min_market_cap: int = 5_000_000_000  # 50億円

    meta = StrategyMeta(
        id="greenblatt",
        name="グリーンブラット（魔法の公式）",
        proposer="ジョエル・グリーンブラット",
        summary=(
            "「割安で良い事業」を機械的に選ぶ2因子モデル。"
            "①益回り（EBIT÷EV）で「割安さ」、"
            "②資本収益率（EBIT÷投下資本）で「事業の質」を測り、"
            "両方の順位を合計したスコアで上位銘柄を等ウェイトで保有する。"
            "感情的な判断を排除し、ルールベースで年次リバランスするシンプルな戦略。"
            "米国で長期的にS&P500を上回った実績が報告されている。"
        ),
        rebalance_hint="年1回（同じ条件で上位銘柄をリプレース）",
        weaknesses=(
            "2〜3年間は市場に負け続けることもあり、途中でやめてしまうと損失確定になりやすい。"
            "のれん・無形資産が大きい企業では投下資本が過小評価され順位が歪む。"
            "日本の会計基準（JGAAP）ではEBITの直接取得が難しく、近似が必要。"
        ),
        references=[
            "株式市場を出し抜け！（ジョエル・グリーンブラット）",
        ],
        report_focuses=[
            ReportFocus("決算短信", "1ページ目サマリー（営業利益）", "EBITの近似として営業利益を使うため数値を確認する"),
            ReportFocus("有価証券報告書", "セグメント情報", "どの事業が投下資本あたりの収益を稼いでいるか確認する"),
            ReportFocus("有価証券報告書", "貸借対照表", "正味運転資本と固定資産の合計（投下資本）の内訳を確認する"),
        ],
    )

    def screen(
        self,
        fin: pd.DataFrame,
        prices: pd.DataFrame,
        as_of: date,
    ) -> list[ScreenResult]:
        latest = (
            fin.sort_values("disclosed_date")
            .groupby("code", sort=False)
            .last()
            .reset_index()
        )
        merged = latest.merge(prices[["code", "close", "name"]], on="code", how="inner")

        rows = []
        for row in merged.itertuples(index=False):
            op = float(row.operating_profit) if row.operating_profit else None  # EBIT近似
            ta = float(row.total_assets) if row.total_assets else None
            eq = float(row.equity) if row.equity else None
            shares = float(row.shares_outstanding) if row.shares_outstanding else None

            if op is None or ta is None or eq is None or shares is None or shares <= 0:
                continue

            market_cap = float(row.close) * shares
            if market_cap < self.min_market_cap:
                continue

            total_liabilities = ta - eq
            # EV = 時価総額 + 有利子負債近似（総負債を代替として使う）
            ev = market_cap + total_liabilities
            if ev <= 0:
                continue

            # 投下資本 = 純資産（簡易近似。正確には正味運転資本+固定資産）
            invested_capital = eq
            if invested_capital <= 0:
                continue

            earnings_yield = op / ev        # 益回り（高いほど割安）
            roc = op / invested_capital     # 資本収益率（高いほど良い事業）

            rows.append({
                "code": row.code,
                "name": row.name or row.code,
                "earnings_yield": earnings_yield,
                "roc": roc,
                "market_cap": market_cap,
                "op": op,
            })

        if not rows:
            return []

        df = pd.DataFrame(rows)
        # 順位（低い数値 = 良い順位）
        df["rank_ey"] = df["earnings_yield"].rank(ascending=False)
        df["rank_roc"] = df["roc"].rank(ascending=False)
        df["rank_sum"] = df["rank_ey"] + df["rank_roc"]
        df = df.sort_values("rank_sum")

        if self.top_n is not None:
            df = df.head(self.top_n)

        return [
            ScreenResult(
                code=r.code,
                name=r.name,
                score=-r.rank_sum,  # 合成順位が小さいほど良いので反転
                metrics={
                    "合成順位": int(r.rank_sum),
                    "益回り順位": int(r.rank_ey),
                    "資本収益率順位": int(r.rank_roc),
                    "益回り": round(r.earnings_yield, 4),
                    "資本収益率": round(r.roc, 4),
                    "時価総額(百万円)": round(r.market_cap / 1_000_000, 0),
                },
                as_of=as_of,
            )
            for r in df.itertuples(index=False)
        ]
