"""高配当・累進配当投資。

高い配当利回りと連続増配・無減配の実績で銘柄を選ぶ。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pandas as pd

from core.strategies.base import ReportFocus, ScreenResult, Strategy, StrategyMeta


@dataclass
class HighDividendStrategy(Strategy):
    """高配当・累進配当投資。

    Args:
        min_yield: 配当利回りの下限
        max_payout_ratio: 配当性向の上限（高すぎると持続性に疑問）
        min_consecutive_div_years: 連続配当年数の下限（データ内での無減配期間）
    """

    min_yield: float = 0.03         # 3%以上
    max_payout_ratio: float = 0.80  # 配当性向80%以下
    min_consecutive_div_years: int = 3

    meta = StrategyMeta(
        id="high_dividend",
        name="高配当・累進配当",
        proposer="（複数の実践者・インカム投資の体系）",
        summary=(
            "高い配当利回りを持ち、かつ配当を維持・増加し続けてきた企業を選ぶ。"
            "配当は利益の一部であり、長期間にわたり増配を続けてきた企業は安定したビジネスモデルと"
            "株主還元意識が高い経営陣を持つと解釈できる。"
            "インカムゲイン（配当収入）とキャピタルゲインの両方を狙えるが、"
            "配当性向が高すぎると業績悪化時に減配リスクが顕在化する。"
            "日本では累進配当方針を明示する企業が増えており、政策保有株売却資金の還元も注目点。"
        ),
        rebalance_hint="年1回（権利落ち後の3〜4月に次年度候補を見直す）",
        weaknesses=(
            "業績が悪化した際に減配・無配に転落するリスクがある（高利回りは危険信号の場合も）。"
            "高配当株は金利上昇局面で相対的な魅力が低下し株価が下落しやすい。"
            "配当を出すために投資を抑制している企業は将来の成長力が低下するリスクがある。"
        ),
        references=[
            "バフェットの株式ポートフォリオ（メアリー・バフェット、デビッド・クラーク）",
            "株主還元ランキング（東証・各証券会社のレポート）",
        ],
        report_focuses=[
            ReportFocus("有価証券報告書", "配当政策・資本政策", "累進配当・増配方針の明示と理由を確認する"),
            ReportFocus("決算短信", "1ページ目（配当予想）", "会社予想の配当金と前期との比較を確認する"),
            ReportFocus("有価証券報告書", "コーポレート・ガバナンスの状況", "政策保有株の削減予定と自社株買い余地を確認する"),
            ReportFocus("決算短信", "キャッシュフロー計算書（営業CF）", "配当の支払い余力として営業CFを確認する"),
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
            div = float(row.div_annual) if row.div_annual else None
            payout = float(row.payout_ratio) if row.payout_ratio else None

            if div is None or div <= 0:
                continue

            close = float(row.close)
            if close <= 0:
                continue

            div_yield = div / close
            if div_yield < self.min_yield:
                continue

            if payout is not None and payout > self.max_payout_ratio:
                continue

            # 連続配当年数：同銘柄の履歴から無減配期間を計算（データが少ない場合はスキップしない）
            # Phase 1 では fin_summary に複数期があれば確認、なければ 0 として許容
            code_hist = fin[fin["code"] == row.code].sort_values("disclosed_date")
            divs_hist = code_hist["div_annual"].dropna().astype(float)
            consecutive = _count_no_cut_years(divs_hist)
            if consecutive < self.min_consecutive_div_years and len(divs_hist) >= self.min_consecutive_div_years:
                continue

            results.append(
                ScreenResult(
                    code=row.code,
                    name=row.name or row.code,
                    score=div_yield,
                    metrics={
                        "配当利回り": round(div_yield, 4),
                        "1株配当(円)": round(div, 2),
                        "配当性向": round(payout, 4) if payout else None,
                        "連続無減配年数(概算)": consecutive,
                    },
                    as_of=as_of,
                )
            )
        return sorted(results, key=lambda r: r.score or 0, reverse=True)


def _count_no_cut_years(divs: pd.Series) -> int:
    """直近から遡って連続無減配の期数を返す（1期ごとを1年換算）。"""
    vals = divs.tolist()
    if not vals:
        return 0
    count = 0
    for i in range(len(vals) - 1, 0, -1):
        if vals[i] >= vals[i - 1]:
            count += 1
        else:
            break
    return count
