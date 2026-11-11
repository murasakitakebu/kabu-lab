"""手法ライブラリ。

全手法のインスタンスを `ALL_STRATEGIES` で参照できる。
スクリーニングで使う場合は各クラスを直接インポートしてパラメータを渡す。
"""

from core.strategies.base import ReportFocus, ScreenResult, Strategy, StrategyMeta
from core.strategies.buffett import BuffettStrategy
from core.strategies.graham import GrahamStrategy
from core.strategies.greenblatt import GreenblattStrategy
from core.strategies.high_dividend import HighDividendStrategy
from core.strategies.lynch import LynchStrategy
from core.strategies.momentum import MomentumStrategy
from core.strategies.tse_reform import TseReformStrategy

#: ID → クラス のマップ（UI のドロップダウンや CLI で使う）
STRATEGY_CLASSES: dict[str, type[Strategy]] = {
    "buffett": BuffettStrategy,
    "graham": GrahamStrategy,
    "greenblatt": GreenblattStrategy,
    "lynch": LynchStrategy,
    "high_dividend": HighDividendStrategy,
    "tse_reform": TseReformStrategy,
    "momentum": MomentumStrategy,
}

__all__ = [
    "ReportFocus",
    "ScreenResult",
    "Strategy",
    "StrategyMeta",
    "BuffettStrategy",
    "GrahamStrategy",
    "GreenblattStrategy",
    "HighDividendStrategy",
    "LynchStrategy",
    "MomentumStrategy",
    "TseReformStrategy",
    "STRATEGY_CLASSES",
]
