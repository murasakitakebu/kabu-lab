"""タスク8：タイムマシン計算機の動作確認スクリプト。

トヨタ(7203)・任天堂(7974)で計算を実行し、結果を出力する。
既知値との照合はプリントした値を目視で確認する。
"""
from datetime import date

from core.calc.timemachine import Request, run
from core.data.store import Store

store = Store(read_only=True)
dr = store.price_date_range()
print("データ期間:", dr)

for ticker, name in [("7203", "トヨタ"), ("7974", "任天堂")]:
    print()
    print(f"=== {name}({ticker}) 2022-01-04 に 100万円一括 ===")
    req = Request(code=ticker, start=date(2022, 1, 4), amount=1_000_000)
    res = run(store, req)
    r = res.main
    print(f"期間: {r.start} 〜 {r.end}")
    print(f"購入株数: {float(r.shares):,.0f}株")
    print(f"取得価額: {r.cost_basis:,}円")
    print(f"評価額(税引前): {r.total_value:,}円")
    print(f"損益: {r.profit:+,}円")
    print(f"トータルリターン: {r.total_return:+.2%}")
    irr = f"{r.irr:+.2%}" if r.irr is not None else "N/A"
    print(f"年率(IRR): {irr}")
    print(f"最大ドローダウン: {r.max_drawdown:.1%}")
    print(f"受取配当(税引後): {r.dividends_gross - r.dividends_tax:,}円")
    print("ベンチマーク:")
    for bname, x in res.benchmarks.items():
        print(f"  {bname}: {x.total_return:+.2%} ({x.total_value:,}円)")
    for n in res.notes:
        print("[注]", n)

store.close()
print()
print("OK")
