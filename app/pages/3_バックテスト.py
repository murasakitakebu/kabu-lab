"""バックテスト：定期リバランス戦略の過去検証。"""

from __future__ import annotations

import os
from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st

from core.backtest.engine import BacktestConfig, BacktestResult, run as run_backtest
from core.data.store import DEFAULT_DB_PATH, Store
from core.strategies import STRATEGY_CLASSES

st.set_page_config(page_title="バックテスト | Kabu Lab", layout="wide")

STRATEGY_NAMES = {
    "buffett": "バフェット流（クオリティ）",
    "graham": "グレアム（ネットネット）",
    "greenblatt": "グリーンブラット（魔法の公式）",
    "lynch": "リンチ（PEG）",
    "high_dividend": "高配当・累進配当",
    "tse_reform": "東証改革テーマ",
    "momentum": "モメンタム",
}

FREQ_LABELS = {"monthly": "月次", "quarterly": "四半期", "yearly": "年次"}
PALETTE = ["#0072B2", "#E69F00", "#009E73"]


@st.cache_resource
def get_store() -> Store:
    return Store(os.environ.get("KABULAB_DB", DEFAULT_DB_PATH), read_only=True)


@st.cache_data(ttl=3600)
def _load_prices(_store: Store, start: date, end: date) -> pd.DataFrame:
    with _store.connect() as con:
        return con.execute(
            "SELECT code, date, open, adj_close FROM daily_bars WHERE date BETWEEN ? AND ?",
            [start, end],
        ).df()


@st.cache_data(ttl=3600)
def _load_fin(_store: Store, end: date) -> pd.DataFrame:
    with _store.connect() as con:
        return con.execute(
            "SELECT * FROM fin_summary WHERE disclosed_date <= ?", [end]
        ).df()


@st.cache_data(ttl=3600)
def _load_listed(_store: Store, end: date) -> pd.DataFrame:
    with _store.connect() as con:
        return con.execute(
            "SELECT as_of, code, name FROM listed_info WHERE as_of <= ?", [end]
        ).df()


@st.cache_data(ttl=3600)
def _load_benchmark(_store: Store, code: str, start: date, end: date) -> pd.DataFrame:
    with _store.connect() as con:
        return con.execute(
            "SELECT date, adj_close FROM daily_bars WHERE code = ? AND date BETWEEN ? AND ?",
            [code, start, end],
        ).df()


def build_equity_curve(
    result: BacktestResult, benchmark_df: pd.DataFrame, initial_cash: float
) -> pd.DataFrame:
    df = result.daily[["date", "portfolio_value"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    df["normalized"] = df["portfolio_value"] / initial_cash
    rows = [
        {"date": r.date, "series": "バックテスト戦略", "value": r.normalized}
        for r in df.itertuples(index=False)
    ]
    if not benchmark_df.empty:
        bm = benchmark_df.copy()
        bm["date"] = pd.to_datetime(bm["date"])
        bm = bm.sort_values("date")
        bm_start = bm["adj_close"].iloc[0]
        for r in bm.itertuples(index=False):
            rows.append({"date": r.date, "series": "TOPIX（1306）", "value": r.adj_close / bm_start})
    return pd.DataFrame(rows)


# ── UI ────────────────────────────────────────────────────────────────────────

st.title("バックテスト")
st.caption("本ツールの出力は学習・検証目的であり、投資助言ではありません。")

try:
    store = get_store()
except Exception as e:
    st.error(f"DB に接続できません: {e}")
    st.stop()

with st.sidebar:
    st.header("設定")

    strategy_id = st.selectbox(
        "手法", options=list(STRATEGY_NAMES.keys()), format_func=lambda k: STRATEGY_NAMES[k]
    )

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("開始日", value=date(2020, 1, 1))
    with col2:
        end_date = st.date_input("終了日", value=date.today() - timedelta(days=1))

    rebalance_freq = st.selectbox(
        "リバランス頻度",
        options=["monthly", "quarterly", "yearly"],
        index=1,
        format_func=lambda k: FREQ_LABELS[k],
    )
    max_holdings = st.slider("最大保有銘柄数", 5, 100, 30, 5)
    initial_cash = (
        st.number_input("初期資金（万円）", min_value=100, max_value=100_000, value=1_000, step=100)
        * 10_000
    )
    commission_rate = st.slider("片道手数料率 (%)", 0.0, 0.5, 0.0, 0.05) / 100

    st.markdown("---")
    st.markdown("**手法パラメータ**")
    params: dict = {}

    if strategy_id == "buffett":
        params["min_roe"] = st.slider("ROE 下限", 0.05, 0.50, 0.15, 0.01, format="%.2f", key="bt_roe")
        params["max_per"] = st.slider("PER 上限（0=制限なし）", 0, 100, 25, 1, key="bt_per")
        if params["max_per"] == 0:
            params["max_per"] = None
        params["max_debt_ratio"] = st.slider("負債比率 上限", 0.5, 5.0, 1.5, 0.1, format="%.1f", key="bt_debt")
    elif strategy_id == "graham":
        params["ncav_ratio"] = st.slider("NCAV 比率 上限", 0.3, 1.0, 0.67, 0.01, format="%.2f", key="bt_ncav")
    elif strategy_id == "greenblatt":
        params["top_n"] = max_holdings
        params["min_market_cap"] = st.number_input("時価総額下限（億円）", 10, 10000, 100, 10, key="bt_mktcap") * 100_000_000
    elif strategy_id == "lynch":
        params["max_peg"] = st.slider("PEG 上限", 0.3, 2.0, 1.0, 0.1, format="%.1f", key="bt_peg")
    elif strategy_id == "high_dividend":
        params["min_yield"] = st.slider("配当利回り 下限", 0.01, 0.10, 0.03, 0.005, format="%.3f", key="bt_yield")
        params["max_payout"] = st.slider("配当性向 上限", 0.3, 1.0, 0.75, 0.05, format="%.2f", key="bt_payout")
    elif strategy_id == "tse_reform":
        params["max_pbr"] = st.slider("PBR 上限", 0.3, 2.0, 1.0, 0.1, format="%.1f", key="bt_pbr")
    elif strategy_id == "momentum":
        params["top_n"] = max_holdings
        params["min_market_cap"] = st.number_input("時価総額下限（億円）", 10, 10000, 100, 10, key="bt_mom_mktcap") * 100_000_000

    run_button = st.button("バックテスト実行", type="primary", use_container_width=True)

if run_button:
    if start_date >= end_date:
        st.error("開始日は終了日より前にしてください。")
        st.stop()

    with st.spinner("バックテスト実行中...（数分かかる場合があります）"):
        try:
            prices_df = _load_prices(store, start_date, end_date)
            fin_df = _load_fin(store, end_date)
            listed_df = _load_listed(store, end_date)

            if prices_df.empty:
                st.warning("指定期間の株価データがありません。scripts/initial_load.py でデータを取得してください。")
                st.stop()

            strategy = STRATEGY_CLASSES[strategy_id](**params)
            config = BacktestConfig(
                start=start_date,
                end=end_date,
                initial_cash=float(initial_cash),
                rebalance_freq=rebalance_freq,
                max_holdings=max_holdings,
                commission_rate=commission_rate,
            )
            result = run_backtest(strategy, config, prices_df, fin_df, listed_df)

            # ベンチマーク（1306: TOPIX ETF）
            bm_df = _load_benchmark(store, "13060", start_date, end_date)
            if bm_df.empty:
                bm_df = _load_benchmark(store, "1306", start_date, end_date)

        except Exception as e:
            st.error(f"エラー: {e}")
            st.stop()

    for w in result.warnings:
        st.warning(w)

    st.subheader("パフォーマンス指標")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("トータルリターン", f"{result.total_return:+.1%}" if result.total_return is not None else "—")
    with c2:
        st.metric("CAGR（年率）", f"{result.cagr:+.2%}" if result.cagr is not None else "—")
    with c3:
        st.metric("最大ドローダウン", f"{result.max_drawdown:.1%}" if result.max_drawdown is not None else "—")
    with c4:
        st.metric("シャープレシオ", f"{result.sharpe:.2f}" if result.sharpe is not None else "—")

    st.subheader("資産推移（初期資金 = 1）")
    equity_df = build_equity_curve(result, bm_df, float(initial_cash))
    if not equity_df.empty:
        chart = (
            alt.Chart(equity_df)
            .mark_line()
            .encode(
                x=alt.X("date:T", title="日付"),
                y=alt.Y("value:Q", title="基準値（1 = 初期資金）"),
                color=alt.Color(
                    "series:N",
                    title=None,
                    scale=alt.Scale(
                        domain=["バックテスト戦略", "TOPIX（1306）"],
                        range=PALETTE[:2],
                    ),
                ),
                tooltip=["date:T", "series:N", alt.Tooltip("value:Q", format=".3f")],
            )
            .properties(height=400)
        )
        st.altair_chart(chart, use_container_width=True)

    if result.annual_returns is not None and not result.annual_returns.empty:
        st.subheader("年別リターン")
        ar = result.annual_returns.reset_index()
        ar.columns = ["年", "リターン"]
        ar["年"] = ar["年"].dt.year
        bar = (
            alt.Chart(ar)
            .mark_bar()
            .encode(
                x=alt.X("年:O", title="年"),
                y=alt.Y("リターン:Q", title="年間リターン", axis=alt.Axis(format=".0%")),
                color=alt.condition(
                    alt.datum["リターン"] >= 0,
                    alt.value(PALETTE[2]),
                    alt.value("#D55E00"),
                ),
                tooltip=["年:O", alt.Tooltip("リターン:Q", format=".1%")],
            )
            .properties(height=250)
        )
        st.altair_chart(bar, use_container_width=True)

    tab1, tab2 = st.tabs(["取引履歴", "保有銘柄履歴"])
    with tab1:
        if result.trades.empty:
            st.info("取引がありませんでした。")
        else:
            st.dataframe(result.trades, use_container_width=True, hide_index=True)
            st.download_button(
                "取引履歴 CSV",
                result.trades.to_csv(index=False, encoding="utf-8-sig"),
                f"trades_{strategy_id}.csv",
                "text/csv",
            )
    with tab2:
        if result.holdings_history.empty:
            st.info("保有銘柄履歴がありません。")
        else:
            st.dataframe(result.holdings_history, use_container_width=True, hide_index=True)
            st.download_button(
                "保有銘柄 CSV",
                result.holdings_history.to_csv(index=False, encoding="utf-8-sig"),
                f"holdings_{strategy_id}.csv",
                "text/csv",
            )

else:
    st.info("左サイドバーで条件を設定し「バックテスト実行」ボタンを押してください。")
    st.markdown("""
**注意事項**
- 表示される結果はすべて過去のデータに基づくシミュレーションです
- 将来の投資成果を示すものではありません
- 取引コスト・税金・スリッページが実際と異なる場合があります
""")
