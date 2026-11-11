"""タイムマシン計算機：あの日に買っていたら、今いくら？"""

from __future__ import annotations

import os
from datetime import timedelta

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

from core.calc.returns import Options, SimResult
from core.calc.timemachine import BENCHMARKS, Request, compare, run
from core.data.jquants import display_code, normalize_code
from core.data.store import DEFAULT_DB_PATH, Store

st.set_page_config(page_title="タイムマシン計算機 | Kabu Lab", layout="wide")

# 色は系列（銘柄・比較対象）ごとに固定する。Okabe-Ito（色覚多様性に配慮した配色）
PALETTE = ["#0072B2", "#E69F00", "#009E73", "#CC79A7", "#D55E00"]
SERIES_COLORS = {
    "この銘柄": PALETTE[0],
    "TOPIX（1306）": PALETTE[1],
    "日経平均（1321）": PALETTE[2],
    "定期預金": PALETTE[3],
    "投資額（元本）": "#8C8C8C",
}


@st.cache_resource
def get_store() -> Store:
    # KABULAB_DB で別の DB を指定できる（画面の動作確認用）
    return Store(os.environ.get("KABULAB_DB", DEFAULT_DB_PATH), read_only=True)


def yen(v: float | int) -> str:
    return f"{v:,.0f} 円"


def pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.2%}"


def line_chart(df: pd.DataFrame, colors: dict[str, str], dashed: set[str]) -> alt.Chart:
    """1 本の縦軸（円）の折れ線。ホバーで縦線と全系列の値を表示する。"""
    order = [s for s in colors if s in set(df["series"])]
    color = alt.Color(
        "series:N",
        title=None,
        scale=alt.Scale(domain=order, range=[colors[s] for s in order]),
        legend=alt.Legend(orient="top"),
    )
    dash = alt.StrokeDash(
        "series:N",
        scale=alt.Scale(domain=order, range=[[4, 3] if s in dashed else [1, 0] for s in order]),
        legend=None,
    )
    base = alt.Chart(df).encode(
        x=alt.X("date:T", title=None, axis=alt.Axis(format="%Y/%m", grid=False)),
        y=alt.Y("value:Q", title="評価額（円）", axis=alt.Axis(format=",.0f", gridOpacity=0.3)),
    )
    lines = base.mark_line(strokeWidth=2).encode(color=color, strokeDash=dash)
    hover = alt.selection_point(fields=["date"], nearest=True, on="pointerover", empty=False)
    wide = df.pivot_table(index="date", columns="series", values="value").reset_index()
    rule = (
        alt.Chart(wide)
        .mark_rule(color="#8C8C8C", strokeWidth=1)
        .encode(
            x="date:T",
            opacity=alt.condition(hover, alt.value(0.8), alt.value(0)),
            tooltip=[alt.Tooltip("date:T", title="日付", format="%Y/%m/%d")]
            + [alt.Tooltip(f"{s}:Q", title=s, format=",.0f") for s in order],
        )
        .add_params(hover)
    )
    points = (
        base.mark_point(size=60, filled=True)
        .encode(color=color, opacity=alt.condition(hover, alt.value(1), alt.value(0)))
        .transform_filter(hover)
    )
    return (lines + rule + points).properties(height=380)


def summary_metrics(r: SimResult) -> None:
    c = st.columns(4)
    c[0].metric("評価額（税引前）", yen(r.total_value), pct(r.total_return))
    c[1].metric("損益", yen(r.profit))
    c[2].metric("年率（IRR）", pct(r.irr))
    c[3].metric("最大下落率", f"{r.max_drawdown:.1%}")
    c = st.columns(4)
    c[0].metric("投資額（元本）", yen(r.invested))
    c[1].metric("全部売った場合の手取り", yen(r.after_tax_value), pct(r.after_tax_return))
    c[2].metric("受取配当（税引後）", yen(r.dividends_gross - r.dividends_tax))
    c[3].metric("保有株数", f"{float(r.shares):,.0f} 株")


# --- 画面 -------------------------------------------------------------------

st.title("タイムマシン計算機")
st.caption("あの日に買っていたら、今いくら？ ※学習・検証目的であり、投資助言ではありません。")

try:
    store = get_store()
    data_range = store.price_date_range()
except duckdb.IOException:
    st.error(
        "データベースが使用中です（データ取得スクリプトの実行中など）。終わってから再読み込みしてください。"
    )
    st.stop()
if data_range is None:
    st.warning(
        "株価データがありません。先に `uv run python scripts/initial_load.py` を実行してください。"
    )
    st.stop()
dmin, dmax = data_range

with st.sidebar:
    st.header("条件")
    mode = st.radio("買い方", ["一括で買う", "毎月積み立てる"], horizontal=True)
    label = "購入日" if mode == "一括で買う" else "積立開始日"
    start = st.date_input(
        label, value=max(dmin, dmax - timedelta(days=365)), min_value=dmin, max_value=dmax
    )
    amount = st.number_input(
        "投資金額（円）" if mode == "一括で買う" else "毎月の金額（円）",
        min_value=1_000,
        value=1_000_000 if mode == "一括で買う" else 30_000,
        step=10_000,
    )
    monthly_day = st.number_input("積立日（毎月）", 1, 31, 1) if mode == "毎月積み立てる" else 1
    end = st.date_input("評価日（売却日）", value=dmax, min_value=dmin, max_value=dmax)
    st.divider()
    reinvest = st.checkbox("配当を再投資する", value=True)
    nisa = st.checkbox("NISA 口座（非課税）", value=False)
    unit = st.checkbox("100 株単位で買う（単元株）", value=False)
    fee = st.number_input("売買手数料（1 回あたり・円）", min_value=0, value=0, step=100)
    rate = st.number_input("定期預金の年利（%）", min_value=0.0, value=0.2, step=0.05) / 100
    st.caption(f"データ期間：{dmin} 〜 {dmax}（J-Quants Free プラン）")

opts = Options(
    lot_size=100 if unit else 1, reinvest_dividends=reinvest, nisa=nisa, fee_per_trade=int(fee)
)


def make_request(code: str) -> Request:
    return Request(
        code=code,
        start=start,
        amount=int(amount),
        end=end,
        mode="lump" if mode == "一括で買う" else "dca",
        monthly_day=int(monthly_day),
        opts=opts,
        deposit_rate=rate,
    )


def pick_security(key: str) -> str | None:
    q = st.text_input("銘柄コード または 社名", key=f"{key}_q", placeholder="例：7203、トヨタ")
    if not q:
        return None
    hits = store.search_securities(q)
    if hits.empty:
        st.info("該当する銘柄がありません")
        return None
    labels = {
        f"{display_code(r.code)} {r.name}（{r.market_name}）": r.code for r in hits.itertuples()
    }
    return labels[st.selectbox("候補", list(labels), key=f"{key}_sel")]


if start >= end:
    st.warning("評価日は購入日（積立開始日）より後にしてください。")
    st.stop()

tab_single, tab_compare = st.tabs(["1 銘柄で計算", "複数銘柄を比較（最大 5）"])

with tab_single:
    code = pick_security("single")
    if code:
        try:
            res = run(store, make_request(code))
        except ValueError as e:
            st.error(str(e))
        else:
            r = res.main
            st.subheader(f"{r.start} 〜 {r.end}")
            summary_metrics(r)
            for n in res.notes:
                st.info(n)
            colors = {**SERIES_COLORS}
            colors[f"定期預金（年{rate:.2%}）"] = colors.pop("定期預金")
            st.altair_chart(
                line_chart(res.chart, colors, dashed={"投資額（元本）"}), width="stretch"
            )
            rows = [("この銘柄", r), *res.benchmarks.items()]
            table = pd.DataFrame(
                [
                    {
                        "": name,
                        "評価額": x.total_value,
                        "トータルリターン": x.total_return,
                        "年率": x.irr,
                        "最大下落率": x.max_drawdown,
                    }
                    for name, x in rows
                ]
            )
            dep_last = res.deposit.iloc[-1]
            table.loc[len(table)] = {
                "": f"定期預金（年{rate:.2%}）",
                "評価額": dep_last["value"],
                "トータルリターン": dep_last["value"] / dep_last["invested"] - 1,
                "年率": None,
                "最大下落率": 0.0,
            }
            st.dataframe(
                table.style.format(
                    {
                        "評価額": "{:,.0f}",
                        "トータルリターン": "{:+.2%}",
                        "年率": lambda v: "—" if pd.isna(v) else f"{v:+.2%}",
                        "最大下落率": "{:.1%}",
                    }
                ),
                hide_index=True,
                width="stretch",
            )
            with st.expander("日次の推移（表）"):
                st.dataframe(r.daily, hide_index=True, width="stretch")

with tab_compare:
    q = st.text_input("銘柄コードをカンマ区切りで（最大 5）", placeholder="例：7203, 7974, 6758")
    codes = [c.strip() for c in q.replace("、", ",").split(",") if c.strip()][:5]
    if codes:
        try:
            results = compare(store, codes, make_request(codes[0]))
        except ValueError as e:
            st.error(str(e))
            results = {}

        def _norm(c: str) -> str | None:
            try:
                return normalize_code(c)
            except ValueError:
                return None

        missing = [c for c in codes if _norm(c) not in results]
        if missing:
            st.warning(f"データがない／見つからない銘柄：{', '.join(missing)}")
        if results:
            names = store.search_securities  # 社名の付与
            label_of = {}
            for c in results:
                hit = names(display_code(c), limit=1)
                label_of[c] = f"{display_code(c)} {hit.iloc[0]['name']}" if not hit.empty else c
            # 色は入力順＝銘柄ごとに固定（5 色まで）
            colors = {label_of[c]: PALETTE[i] for i, c in enumerate(results)}
            colors["投資額（元本）"] = "#8C8C8C"
            first = next(iter(results.values()))
            chart = pd.concat(
                [
                    *[
                        x.daily[["date", "value"]].assign(series=label_of[c])
                        for c, x in results.items()
                    ],
                    first.daily[["date", "invested"]]
                    .rename(columns={"invested": "value"})
                    .assign(series="投資額（元本）"),
                ],
                ignore_index=True,
            )
            chart["date"] = pd.to_datetime(chart["date"])
            st.altair_chart(line_chart(chart, colors, dashed={"投資額（元本）"}), width="stretch")
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "銘柄": label_of[c],
                            "評価額": x.total_value,
                            "トータルリターン": x.total_return,
                            "年率": x.irr,
                            "最大下落率": x.max_drawdown,
                            "受取配当（税引後）": x.dividends_gross - x.dividends_tax,
                        }
                        for c, x in results.items()
                    ]
                ).style.format(
                    {
                        "評価額": "{:,.0f}",
                        "トータルリターン": "{:+.2%}",
                        "年率": lambda v: "—" if pd.isna(v) else f"{v:+.2%}",
                        "最大下落率": "{:.1%}",
                        "受取配当（税引後）": "{:,.0f}",
                    }
                ),
                hide_index=True,
                width="stretch",
            )

st.divider()
st.caption(
    "計算の前提：未調整終値と実際の株数で日次計算。分割・併合を反映。配当は決算短信からの推定値"
    "（権利落ち日は T+2 で算出、支払日は基準日の約 75 日後と近似）。税率 20.315%。"
    f" 比較指数は {', '.join(BENCHMARKS)} の価格で近似。"
)
