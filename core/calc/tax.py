"""税金の計算（上場株式の配当・譲渡益、預金利子）。

税率 20.315% = 所得税 15%＋復興特別所得税 0.315%（計 15.315%）＋住民税 5%。
源泉徴収・特定口座では所得税分と住民税分をそれぞれ円未満切り捨てで計算するので、それに合わせる。
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

NATIONAL_RATE = Decimal("0.15315")  # 所得税＋復興特別所得税
LOCAL_RATE = Decimal("0.05")  # 住民税
TOTAL_RATE = NATIONAL_RATE + LOCAL_RATE  # 0.20315


def _floor_yen(x: Decimal) -> int:
    return int(x.to_integral_value(rounding=ROUND_FLOOR))


def tax_on_income(amount: int | Decimal, *, nisa: bool = False) -> int:
    """配当・譲渡益・利子にかかる税額（円）。0 以下・NISA なら 0。"""
    amount = Decimal(amount)
    if nisa or amount <= 0:
        return 0
    return _floor_yen(amount * NATIONAL_RATE) + _floor_yen(amount * LOCAL_RATE)
