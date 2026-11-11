"""グレアム（ネットネット投資）。

時価総額が「流動資産 − 総負債」を下回る超割安株を狙う。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from core.strategies.base import ReportFocus, ScreenResult, Strategy, StrategyMeta

# J-Quants fin_summary の shares_outstanding は発行済み株式数（FY末時点）。
# 株価 × 発行済み株式数 ≈ 時価総額の近似として使う。


@dataclass
class GrahamStrategy(Strategy):
    """グレアム ネットネット投資。

    Args:
        max_ncav_ratio: 時価総額 / NCAV の上限（1.0 未満が「ネットネット」の定義。
                        0.67 以下をグレアムは推奨）
    """

    max_ncav_ratio: float = 1.0

    meta = StrategyMeta(
        id="graham",
        name="グレアム（ネットネット）",
        proposer="ベンジャミン・グレアム",
        summary=(
            "「ネット・カレント・アセット・バリュー（NCAV）」＝流動資産−総負債が時価総額を上回る企業を買う。"
            "清算価値以下で株を買うという究極の安全マージン重視の手法。"
            "事業の将来性ではなく貸借対照表の現在価値だけを根拠にするため、"
            "市場が極端に悲観的になった局面や小型株に多く出現する。"
            "日本では個人株主に還元しない内部留保の厚い企業が多く、"
            "東証改革以降は候補が減少傾向にある。"
        ),
        rebalance_hint="半年〜1年ごとに条件再チェック（NCAV比率の変化でリプレース）",
        weaknesses=(
            "バリュートラップ（安くても上がらない）になりやすい。"
            "事業の収益性を問わないため、赤字継続や不正リスクがある。"
            "候補が市場全体で少ないため分散が難しい。"
        ),
        references=[
            "賢明なる投資家（ベンジャミン・グレアム）",
            "証券分析（グレアム＆ドッド）",
        ],
        report_focuses=[
            ReportFocus("有価証券報告書", "貸借対照表（流動資産・流動負債）", "NCAV の計算根拠となる流動資産の中身を確認する"),
            ReportFocus("有価証券報告書", "注記（偶発債務・訴訟）", "オフバランスの負債がないか確認する"),
            ReportFocus("有価証券報告書", "大株主の状況", "経営陣による資本の固定化・持ち合いの有無を確認する"),
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

        results = []
        for row in merged.itertuples(index=False):
            ta = float(row.total_assets) if row.total_assets else None
            eq = float(row.equity) if row.equity else None
            shares = float(row.shares_outstanding) if row.shares_outstanding else None

            if ta is None or eq is None or shares is None or shares <= 0:
                continue

            # 流動資産の近似：総資産 − 純資産（概算。J-Quants Free は流動資産単体を返さない）
            # より正確には有報の貸借対照表が必要だが、Phase 1 データでの近似として使う
            current_assets_approx = ta - eq  # ≒ 総負債（過大評価になるため保守的）
            # 本来の NCAV = 流動資産 − 総負債。ここでは「純資産 < 流動資産と仮定」が成立しないため
            # fin_summary で取れる範囲の近似として eq を流動資産代替に使う
            # （Phase 3 で EDINET 連携時に正確な値に差し替える）
            total_liabilities = ta - eq
            ncav = eq - total_liabilities  # 純資産 − 総負債（保守的な NCAV 近似）

            if ncav <= 0:
                continue

            market_cap = float(row.close) * shares
            ncav_ratio = market_cap / ncav

            if ncav_ratio > self.max_ncav_ratio:
                continue

            results.append(
                ScreenResult(
                    code=row.code,
                    name=row.name or row.code,
                    score=1.0 / ncav_ratio if ncav_ratio > 0 else None,
                    metrics={
                        "NCAV近似(百万円)": round(ncav / 1_000_000, 0),
                        "時価総額(百万円)": round(market_cap / 1_000_000, 0),
                        "時価総額/NCAV": round(ncav_ratio, 3),
                    },
                    as_of=as_of,
                )
            )
        return sorted(results, key=lambda r: r.score or 0, reverse=True)
