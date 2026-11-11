"""手法別の「読み方ナビ」マップ定義。

各手法ごとに「有報・決算短信のどこを読むべきか」「何がわかるか」「赤信号チェック」を
データとして保持する。UI 層（Streamlit）はここからデータを取得して表示する。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReadingPoint:
    """有報・短信の「読むべき箇所」の定義。"""

    section: str          # 書類・箇所の名前
    what_you_learn: str   # 何がわかるか
    strategies: list[str]  # 特に重要な手法のキー
    priority: int = 2     # 1=必須 2=推奨 3=参考
    xbrl_element: str | None = None  # XBRL 要素名（将来の自動抽出用）


@dataclass
class RedFlagCheck:
    """財務的な赤信号チェック項目。"""

    label: str
    description: str
    xbrl_element: str | None = None


@dataclass
class StrategyReadingGuide:
    """1手法分の読み方ガイド。"""

    strategy_key: str
    strategy_name: str
    reading_points: list[ReadingPoint]
    red_flags: list[RedFlagCheck] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# 読むべき箇所マップ（全手法共通）
# ---------------------------------------------------------------------------

COMMON_READING_POINTS: list[ReadingPoint] = [
    ReadingPoint(
        section="決算短信 1ページ目サマリー",
        what_you_learn="業績・予想・進捗率。前期比で増収増益かどうかを一目で確認できる。",
        strategies=["buffett", "graham", "greenblatt", "lynch", "high_dividend", "tse_reform", "momentum"],
        priority=1,
    ),
    ReadingPoint(
        section="事業等のリスク",
        what_you_learn="経営陣が認識する主要リスク。新しいリスク項目の追加・削除に注目する。",
        strategies=["buffett", "graham", "greenblatt", "lynch", "high_dividend", "tse_reform", "momentum"],
        priority=2,
    ),
    ReadingPoint(
        section="キャッシュフロー計算書",
        what_you_learn="利益の質（営業CF vs 純利益）。CFが伴わない利益増には要注意。",
        strategies=["buffett", "graham", "greenblatt", "lynch", "high_dividend", "tse_reform", "momentum"],
        priority=1,
        xbrl_element="NetCashProvidedByUsedInOperatingActivities",
    ),
    ReadingPoint(
        section="注記（継続企業の前提・偶発債務・関連当事者取引）",
        what_you_learn="財務上の赤信号の有無。継続企業注記があれば倒産リスクが高い。",
        strategies=["buffett", "graham", "greenblatt", "lynch", "high_dividend", "tse_reform", "momentum"],
        priority=1,
    ),
]

# ---------------------------------------------------------------------------
# 手法別ガイド定義
# ---------------------------------------------------------------------------

STRATEGY_GUIDES: dict[str, StrategyReadingGuide] = {
    "buffett": StrategyReadingGuide(
        strategy_key="buffett",
        strategy_name="バフェット流（クオリティ投資）",
        summary=(
            "長期的に高い ROE を維持できる「堀（モート）」を持つ企業を探す。"
            "事業の中身・競争優位・経営者の資質を重点的に読む。"
        ),
        reading_points=[
            ReadingPoint(
                section="事業の内容",
                what_you_learn="ビジネスモデルと競争優位（堀）の理解。10年後も同じビジネスをしているか。",
                strategies=["buffett"],
                priority=1,
            ),
            ReadingPoint(
                section="経営者による分析（MD&A）",
                what_you_learn="課題認識と戦略。経営者が正直に弱点を語っているか。",
                strategies=["buffett", "lynch"],
                priority=1,
            ),
            ReadingPoint(
                section="セグメント情報",
                what_you_learn="どの事業が稼いでいるか。収益の安定性と集中度。",
                strategies=["buffett", "lynch"],
                priority=1,
            ),
            ReadingPoint(
                section="設備投資・研究開発費の推移",
                what_you_learn="競争優位維持に必要な投資の水準。高い再投資需要はROEを下げる。",
                strategies=["buffett"],
                priority=2,
            ),
        ] + COMMON_READING_POINTS,
        red_flags=[
            RedFlagCheck("ROE の急低下", "10年平均ROEが15%を下回り始めた場合は堀の消失を疑う"),
            RedFlagCheck("継続企業注記", "監査人から継続企業の前提に疑義が示されている"),
            RedFlagCheck("CFと純利益の乖離", "営業CFが純利益を大きく下回る状態が続く"),
        ],
    ),
    "graham": StrategyReadingGuide(
        strategy_key="graham",
        strategy_name="グレアム（ネットネット）",
        summary=(
            "時価総額が「流動資産 − 総負債」（NCAV）を下回る超割安株を探す。"
            "清算価値に着目するため、貸借対照表の流動資産・負債の内訳が核心。"
        ),
        reading_points=[
            ReadingPoint(
                section="貸借対照表（流動資産・負債）",
                what_you_learn="清算価値の計算。現金・売掛金・棚卸資産の内訳と質を確認。",
                strategies=["graham", "tse_reform"],
                priority=1,
                xbrl_element="CurrentAssets",
            ),
            ReadingPoint(
                section="棚卸資産の注記",
                what_you_learn="在庫の評価方法と陳腐化リスク。NCAV 計算で棚卸に割引を適用する場合に参照。",
                strategies=["graham"],
                priority=2,
            ),
            ReadingPoint(
                section="売掛金の注記（貸倒引当金）",
                what_you_learn="売掛金の回収可能性。引当金が薄すぎる場合は実質 NCAV が低い。",
                strategies=["graham"],
                priority=2,
            ),
        ] + COMMON_READING_POINTS,
        red_flags=[
            RedFlagCheck("継続的な営業赤字", "NCAV が低くても赤字継続で消耗する可能性"),
            RedFlagCheck("棚卸の急増", "売れ残り在庫が膨らんでいないか"),
        ],
    ),
    "greenblatt": StrategyReadingGuide(
        strategy_key="greenblatt",
        strategy_name="グリーンブラット（魔法の公式）",
        summary=(
            "益回り（EBIT/EV）と資本収益率（EBIT/使用資本）の合成ランキングで銘柄を選ぶ。"
            "のれん・無形資産の扱いと、一時的利益の除去が重要。"
        ),
        reading_points=[
            ReadingPoint(
                section="損益計算書（営業利益・特別損益）",
                what_you_learn="EBIT の質。特別利益・損失を除いた継続的な稼ぐ力を把握する。",
                strategies=["greenblatt"],
                priority=1,
            ),
            ReadingPoint(
                section="のれん・無形資産の注記",
                what_you_learn="使用資本の計算に影響。魔法の公式ではのれんを除く場合がある。",
                strategies=["greenblatt"],
                priority=2,
            ),
            ReadingPoint(
                section="セグメント情報",
                what_you_learn="セグメント別の資本効率。低収益セグメントがドラッグになっていないか。",
                strategies=["greenblatt", "buffett"],
                priority=2,
            ),
        ] + COMMON_READING_POINTS,
        red_flags=[
            RedFlagCheck("一時益による EBIT 水増し", "特別利益が継続的に計上されていないか"),
            RedFlagCheck("のれん減損リスク", "M&A で積み上がったのれんが資産の大部分を占める"),
        ],
    ),
    "lynch": StrategyReadingGuide(
        strategy_key="lynch",
        strategy_name="リンチ（PEG）",
        summary=(
            "PER を EPS 成長率で割った PEG 比率が 1 以下の成長株を探す。"
            "成長ドライバーの持続性と、成長を支えるセグメント構造を読む。"
        ),
        reading_points=[
            ReadingPoint(
                section="経営者による分析（MD&A）",
                what_you_learn="成長ドライバーの説明と課題認識。次の1〜3年の成長根拠を経営者が語っているか。",
                strategies=["lynch", "buffett"],
                priority=1,
            ),
            ReadingPoint(
                section="業績予想の修正理由",
                what_you_learn="上振れ・下振れの要因。予想の精度と経営の透明性を確認。",
                strategies=["lynch", "momentum"],
                priority=1,
            ),
            ReadingPoint(
                section="セグメント情報",
                what_you_learn="どのセグメントが成長を牽引しているか。ストーリーと整合するか確認。",
                strategies=["lynch", "buffett"],
                priority=1,
            ),
        ] + COMMON_READING_POINTS,
        red_flags=[
            RedFlagCheck("成長率の急低下", "EPS 成長が鈍化し PEG が割高になっていないか"),
            RedFlagCheck("予想の下方修正が続く", "経営の見通し精度が低い可能性"),
        ],
    ),
    "high_dividend": StrategyReadingGuide(
        strategy_key="high_dividend",
        strategy_name="高配当・累進配当",
        summary=(
            "高い配当利回りを維持できる財務力と、累進配当方針を持つ企業を探す。"
            "配当方針・配当性向・フリーCFに着目する。"
        ),
        reading_points=[
            ReadingPoint(
                section="配当政策・資本政策",
                what_you_learn="配当方針（累進・DOE・性向）、自社株買い計画、株主還元の姿勢。",
                strategies=["high_dividend"],
                priority=1,
            ),
            ReadingPoint(
                section="キャッシュフロー計算書",
                what_you_learn="配当を支える営業CF。配当性向が100%超の場合は借入か資産売却で払っている。",
                strategies=["high_dividend"],
                priority=1,
                xbrl_element="NetCashProvidedByUsedInOperatingActivities",
            ),
            ReadingPoint(
                section="財務状況（有利子負債・自己資本比率）",
                what_you_learn="財務の安定性。高配当でも借金が増え続ける企業は減配リスクが高い。",
                strategies=["high_dividend"],
                priority=1,
            ),
        ] + COMMON_READING_POINTS,
        red_flags=[
            RedFlagCheck("配当性向が100%超", "利益以上の配当は持続不可能"),
            RedFlagCheck("フリーCFが継続赤字", "配当を支えるキャッシュが不足している"),
            RedFlagCheck("有利子負債の急増", "配当維持のために借入を増やしている可能性"),
        ],
    ),
    "tse_reform": StrategyReadingGuide(
        strategy_key="tse_reform",
        strategy_name="東証改革テーマ（PBR改善）",
        summary=(
            "東証の「資本コスト・株価を意識した経営」要請に応じてPBR改善を進める企業を探す。"
            "政策保有株・余剰キャッシュ・自社株買い余地がポイント。"
        ),
        reading_points=[
            ReadingPoint(
                section="大株主の状況",
                what_you_learn="持ち合い株の規模とアクティビストの存在。外国人持株比率も確認。",
                strategies=["tse_reform"],
                priority=1,
            ),
            ReadingPoint(
                section="政策保有株式（コーポレート・ガバナンスの状況）",
                what_you_learn="政策保有株の縮減計画と売却余地。資本効率改善の「のりしろ」を把握。",
                strategies=["tse_reform"],
                priority=1,
            ),
            ReadingPoint(
                section="貸借対照表（流動資産・負債）",
                what_you_learn="ネットキャッシュの水準。現預金 − 有利子負債が大きい企業は株主還元余地が大きい。",
                strategies=["tse_reform", "graham"],
                priority=1,
            ),
            ReadingPoint(
                section="配当政策・資本政策",
                what_you_learn="自社株買いの実施状況と今後の計画。ROE・PBR 目標値の開示有無。",
                strategies=["tse_reform", "high_dividend"],
                priority=1,
            ),
        ] + COMMON_READING_POINTS,
        red_flags=[
            RedFlagCheck("PBR 改善計画の形骸化", "目標は掲げるが具体的施策がない"),
            RedFlagCheck("政策保有株の実質的な縮減なし", "毎年同じ銘柄を「保有継続」と記載"),
        ],
    ),
    "momentum": StrategyReadingGuide(
        strategy_key="momentum",
        strategy_name="モメンタム",
        summary=(
            "過去12ヶ月（直近1ヶ月除く）のリターン上位株を保有するモメンタム戦略。"
            "決算・業績修正の内容がモメンタム継続に影響するため短信の確認が重要。"
        ),
        reading_points=[
            ReadingPoint(
                section="業績予想の修正理由",
                what_you_learn="上振れ修正は継続モメンタムのサイン。下振れ修正はトレンド転換の警戒信号。",
                strategies=["momentum", "lynch"],
                priority=1,
            ),
            ReadingPoint(
                section="決算短信 1ページ目サマリー",
                what_you_learn="業績のサプライズ度合い。予想比での上振れ・下振れが株価モメンタムを変える。",
                strategies=["momentum"],
                priority=1,
            ),
        ] + COMMON_READING_POINTS,
        red_flags=[
            RedFlagCheck("連続下方修正", "業績モメンタムの喪失＝株価モメンタムの終わりのサイン"),
            RedFlagCheck("社長・CFOの突然交代", "経営の不連続性がモメンタム継続リスクに"),
        ],
    ),
}


def get_guide(strategy_key: str) -> StrategyReadingGuide | None:
    """手法キーに対応するガイドを返す。"""
    return STRATEGY_GUIDES.get(strategy_key)


def get_all_reading_points(strategy_key: str) -> list[ReadingPoint]:
    """手法ごとの読むべき箇所リスト（優先度順）を返す。"""
    guide = get_guide(strategy_key)
    if guide is None:
        return []
    # 重複除去（COMMON_READING_POINTS が各手法に含まれているため）
    seen: set[str] = set()
    unique: list[ReadingPoint] = []
    for rp in guide.reading_points:
        if rp.section not in seen:
            seen.add(rp.section)
            unique.append(rp)
    return sorted(unique, key=lambda x: x.priority)


def get_common_red_flags() -> list[RedFlagCheck]:
    """全手法共通の赤信号チェック項目を返す。"""
    return [
        RedFlagCheck(
            "継続企業の前提注記",
            "監査人が継続企業の前提に重要な疑義を示している（Going Concern）",
        ),
        RedFlagCheck(
            "監査人の交代",
            "Big4 → 中小監査法人への交代は財務リスクの高まりを示唆することがある",
        ),
        RedFlagCheck(
            "営業CF 赤字の継続",
            "3期連続で営業キャッシュフローが赤字の場合は事業の収益性に問題がある",
        ),
        RedFlagCheck(
            "偶発債務",
            "訴訟・保証債務など貸借対照表に計上されていないリスクを注記で確認",
        ),
        RedFlagCheck(
            "関連当事者取引の異常",
            "オーナーや役員への不透明な資金流出がないか確認",
        ),
    ]
