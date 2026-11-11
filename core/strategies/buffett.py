"""バフェット流（クオリティ投資）。

高いROEが継続し、低負債で適正な価格の企業を選ぶ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from core.strategies.base import ReportFocus, ScreenResult, Strategy, StrategyMeta


@dataclass
class BuffettStrategy(Strategy):
    """バフェット流クオリティ投資。

    Args:
        min_roe: ROE の下限（直近期）
        max_per: PER の上限（None なら制限なし）
        max_debt_ratio: 負債比率（総負債 / 純資産）の上限
    """

    min_roe: float = 0.15
    max_per: float | None = 25.0
    max_debt_ratio: float = 1.5

    meta = StrategyMeta(
        id="buffett",
        name="バフェット流（クオリティ）",
        proposer="ウォーレン・バフェット / チャーリー・マンガー",
        summary=(
            "「適正な価格の素晴らしい企業」を買い続ける。"
            "高いROEが長期間持続する企業は、競合他社が容易に侵食できない経済的護城河（モート）を持つ証拠。"
            "低負債で自己資本を高効率に活用し、フリーキャッシュフローを安定的に生み出す企業を、"
            "PERなどで適正範囲と判断される価格で取得する。"
            "単純なグレアム流の割安株投資から発展し、「質」に対してプレミアムを支払うことを厭わない。"
        ),
        rebalance_hint="年1回程度（企業のファンダメンタルズが変わらない限り長期保有）",
        weaknesses=(
            "高成長・高ROEに市場がすでに高いバリュエーションを付けており割安感がない局面では成績が低迷しやすい。"
            "金利上昇局面でグロース株全般が売られるとき影響を受ける。"
            "日本株では護城河が明確な企業が少なく、候補が絞られる。"
        ),
        references=[
            "バフェットからの手紙（ローレンス・カニンガム編）",
            "スノーボール（アリス・シュローダー）",
            "マンガーの投資術（トレン・グリフィン）",
        ],
        report_focuses=[
            ReportFocus("有価証券報告書", "事業の内容", "ビジネスモデルと護城河の有無を確認する"),
            ReportFocus("有価証券報告書", "経営者による分析（MD&A）", "ROEの変化要因と経営陣の資本配分方針を読む"),
            ReportFocus("有価証券報告書", "セグメント情報", "どの事業が利益を稼いでいるか、集中度を把握する"),
            ReportFocus("決算短信", "キャッシュフロー計算書", "営業CFと純利益の乖離で利益の質を確認する"),
        ],
    )

    def screen(
        self,
        fin: pd.DataFrame,
        prices: pd.DataFrame,
        as_of: date,
    ) -> list[ScreenResult]:
        # 銘柄ごとに直近1期を取る
        latest = (
            fin.sort_values("disclosed_date")
            .groupby("code", sort=False)
            .last()
            .reset_index()
        )
        merged = latest.merge(prices[["code", "close", "name"]], on="code", how="inner")

        results = []
        for row in merged.itertuples(index=False):
            eq = float(row.equity) if row.equity else None
            ta = float(row.total_assets) if row.total_assets else None
            np_ = float(row.net_profit) if row.net_profit else None

            if not eq or eq <= 0 or np_ is None:
                continue

            roe = np_ / eq
            if roe < self.min_roe:
                continue

            # 負債比率
            if ta is not None:
                debt = ta - eq
                debt_ratio = debt / eq if eq > 0 else None
                if debt_ratio is not None and debt_ratio > self.max_debt_ratio:
                    continue
            else:
                debt_ratio = None

            # PER
            eps = float(row.eps) if row.eps else None
            per = float(row.close) / eps if eps and eps > 0 else None
            if self.max_per is not None and per is not None and per > self.max_per:
                continue

            results.append(
                ScreenResult(
                    code=row.code,
                    name=row.name or row.code,
                    score=roe,
                    metrics={
                        "ROE": round(roe, 4),
                        "PER": round(per, 2) if per else None,
                        "負債比率": round(debt_ratio, 2) if debt_ratio is not None else None,
                        "純利益(百万円)": round(np_ / 1_000_000, 0) if np_ else None,
                    },
                    as_of=as_of,
                )
            )
        return sorted(results, key=lambda r: r.score or 0, reverse=True)
