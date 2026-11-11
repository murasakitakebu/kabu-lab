"""東証改革テーマ（PBR改善・資本効率向上）。

PBR が低く、ネットキャッシュが厚い「資本効率改善余地の大きい企業」を狙う。
東証の要請（2023年〜）に応じた自社株買い・政策保有株売却の受益者となる銘柄を選ぶ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd

from core.strategies.base import ReportFocus, ScreenResult, Strategy, StrategyMeta


@dataclass
class TseReformStrategy(Strategy):
    """東証改革テーマ（PBR改善・自社株買い余地）。

    Args:
        max_pbr: PBR の上限（1.0 未満が東証要請の対象）
        min_net_cash_ratio: ネットキャッシュ / 時価総額の下限（現金豊富さの指標）
    """

    max_pbr: float = 1.0
    min_net_cash_ratio: float = 0.0  # デフォルトは制限なし（PBR < 1 だけで十分）

    meta = StrategyMeta(
        id="tse_reform",
        name="東証改革テーマ（PBR改善）",
        proposer="東京証券取引所（2023年要請）／アクティビスト投資家",
        summary=(
            "2023年3月の東証による「PBR1倍割れ企業への改善要請」を受け、"
            "低PBR・低ROEの企業が自社株買いや政策保有株の売却を通じて株主価値を向上させるテーマ。"
            "ネットキャッシュ（現預金 − 有利子負債）が潤沢な企業は、"
            "余剰資本の活用が株価を押し上げる「カタリスト」になりやすい。"
            "特にプライム市場の時価総額下位・非上場子会社を持つ持ち株会社・"
            "政策保有株が多い企業に注目する。"
        ),
        rebalance_hint="半年ごと（開示の進捗と株価の変化を確認してリプレース）",
        weaknesses=(
            "改善の実行を伴わないコミットメントだけでは株価上昇が長続きしない。"
            "低PBRの原因が事業の構造的な問題（衰退産業・競合激化）の場合はバリュートラップになる。"
            "全体的な市場下落局面では「割安でも下がる」。"
        ),
        references=[
            "東証「資本コストや株価を意識した経営の実現に向けた対応について」（2023年3月）",
            "コーポレートガバナンス・コード（金融庁・東証）",
        ],
        report_focuses=[
            ReportFocus("有価証券報告書", "大株主の状況", "政策保有株の規模と主要な保有先を確認する"),
            ReportFocus("有価証券報告書", "コーポレート・ガバナンスの状況", "政策保有株削減計画・自社株買い枠・ROE目標を確認する"),
            ReportFocus("有価証券報告書", "貸借対照表（現預金・有利子負債）", "ネットキャッシュの水準を確認する"),
            ReportFocus("決算短信", "1ページ目（自己資本比率・BPS）", "BPSと株価の乖離（PBR）を素早く把握する"),
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
            bps = float(row.bps) if row.bps else None
            eq = float(row.equity) if row.equity else None
            shares = float(row.shares_outstanding) if row.shares_outstanding else None
            cash_eq = float(row.cash_eq) if row.cash_eq else None

            if bps is None or bps <= 0:
                continue

            close = float(row.close)
            pbr = close / bps
            if pbr > self.max_pbr:
                continue

            # ネットキャッシュ：現預金等（cash_eq）から有利子負債は取れないので
            # 現預金 / 時価総額の比率を代替指標として使う
            market_cap = close * shares if shares else None
            cash_ratio = (cash_eq / market_cap) if cash_eq and market_cap and market_cap > 0 else None

            if self.min_net_cash_ratio > 0 and (cash_ratio is None or cash_ratio < self.min_net_cash_ratio):
                continue

            results.append(
                ScreenResult(
                    code=row.code,
                    name=row.name or row.code,
                    score=1.0 / pbr if pbr > 0 else None,
                    metrics={
                        "PBR": round(pbr, 3),
                        "BPS(円)": round(bps, 2),
                        "現預金/時価総額": round(cash_ratio, 3) if cash_ratio else None,
                        "時価総額(百万円)": round(market_cap / 1_000_000, 0) if market_cap else None,
                    },
                    as_of=as_of,
                )
            )
        return sorted(results, key=lambda r: r.score or 0, reverse=True)
