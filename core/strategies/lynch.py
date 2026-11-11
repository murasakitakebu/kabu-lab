"""リンチ（PEG レシオ）。

PER を EPS 成長率で割った PEG 比率が1未満の成長株を狙う。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from core.strategies.base import ReportFocus, ScreenResult, Strategy, StrategyMeta


@dataclass
class LynchStrategy(Strategy):
    """リンチ PEG 投資。

    Args:
        max_peg: PEG レシオの上限（1.0 がリンチの基準。0.5 以下は「割安な成長株」）
        min_eps_growth: EPS 成長率の下限（高成長株を対象とするため）
    """

    max_peg: float = 1.0
    min_eps_growth: float = 0.05  # 5%以上の成長を要求

    meta = StrategyMeta(
        id="lynch",
        name="リンチ（PEG）",
        proposer="ピーター・リンチ",
        summary=(
            "PER（株価収益率）を EPS 成長率で割った PEG 比率が1以下の銘柄は「割安な成長株」とみなす。"
            "PER が高くても成長率がそれ以上に高ければ割高ではないという考え方。"
            "フィデリティのマゼランファンドを13年間で年率29%以上で運用したリンチが実践した手法で、"
            "身の回りの生活から有望な会社を見つける「フットワーク投資」の哲学とセットで語られる。"
        ),
        rebalance_hint="半年〜1年ごと（EPS成長率の見直しに合わせる）",
        weaknesses=(
            "EPS 成長率の推定に将来予想（会社予想）を使うと楽観バイアスがかかりやすい。"
            "高成長が続かなかった場合に株価が大幅に調整するリスクがある（成長株の宿命）。"
            "赤字企業や EPS がマイナスの企業は PEG が定義できず候補から外れる。"
        ),
        references=[
            "ピーター・リンチの株で勝つ（ピーター・リンチ）",
            "ピーター・リンチの株式投資の法則（ピーター・リンチ）",
        ],
        report_focuses=[
            ReportFocus("決算短信", "業績予想", "会社予想 EPS と実績の乖離を確認し、成長率推定の前提を見直す"),
            ReportFocus("決算短信", "業績予想の修正理由", "上振れ・下振れの要因が一時的か構造的かを判断する"),
            ReportFocus("有価証券報告書", "経営者による分析（MD&A）", "成長ドライバーの持続性と経営者の見通しを確認する"),
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
            eps = float(row.eps) if row.eps else None
            f_eps = float(row.f_eps) if row.f_eps else None

            if eps is None or eps <= 0 or f_eps is None or f_eps <= 0:
                continue

            eps_growth = (f_eps - eps) / eps
            if eps_growth < self.min_eps_growth:
                continue

            per = float(row.close) / eps
            if per <= 0:
                continue

            peg = per / (eps_growth * 100)  # 成長率を%表示に換算してから割る
            if peg > self.max_peg:
                continue

            results.append(
                ScreenResult(
                    code=row.code,
                    name=row.name or row.code,
                    score=1.0 / peg if peg > 0 else None,
                    metrics={
                        "PEG": round(peg, 3),
                        "PER": round(per, 2),
                        "EPS成長率(予想)": round(eps_growth, 4),
                        "EPS(実績)": round(eps, 2),
                        "EPS(予想)": round(f_eps, 2),
                    },
                    as_of=as_of,
                )
            )
        return sorted(results, key=lambda r: r.score or 0, reverse=True)
