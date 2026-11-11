"""スクリーナー：手法に基づく銘柄絞り込み。"""

from __future__ import annotations

import os
from datetime import date, timedelta

import pandas as pd
import streamlit as st

from core.data.store import DEFAULT_DB_PATH, Store
from core.strategies import STRATEGY_CLASSES
from core.strategies.momentum import MomentumStrategy

st.set_page_config(page_title="スクリーナー | Kabu Lab", layout="wide")

STRATEGY_NAMES = {
    "buffett": "バフェット流（クオリティ）",
    "graham": "グレアム（ネットネット）",
    "greenblatt": "グリーンブラット（魔法の公式）",
    "lynch": "リンチ（PEG）",
    "high_dividend": "高配当・累進配当",
    "tse_reform": "東証改革テーマ",
    "momentum": "モメンタム",
}


@st.cache_resource
def get_store() -> Store:
    return Store(os.environ.get("KABULAB_DB", DEFAULT_DB_PATH), read_only=True)


@st.cache_data(ttl=3600)
def load_listed_info(_store: Store, as_of: date) -> pd.DataFrame:
    with _store.connect() as con:
        return con.execute(
            "SELECT DISTINCT ON (code) code, name FROM listed_info "
            "WHERE as_of <= ? ORDER BY code, as_of DESC",
            [as_of],
        ).df()


@st.cache_data(ttl=3600)
def load_latest_prices(_store: Store, as_of: date) -> pd.DataFrame:
    with _store.connect() as con:
        return con.execute(
            "SELECT DISTINCT ON (code) code, adj_close AS close FROM daily_bars "
            "WHERE date <= ? ORDER BY code, date DESC",
            [as_of],
        ).df()


@st.cache_data(ttl=3600)
def load_fin_summary(_store: Store, as_of: date) -> pd.DataFrame:
    with _store.connect() as con:
        return con.execute(
            "SELECT * FROM fin_summary WHERE disclosed_date <= ?",
            [as_of],
        ).df()


@st.cache_data(ttl=3600)
def load_price_history(_store: Store, as_of: date, days: int = 400) -> pd.DataFrame:
    start = as_of - timedelta(days=days)
    with _store.connect() as con:
        return con.execute(
            "SELECT code, date, adj_close AS close FROM daily_bars "
            "WHERE date BETWEEN ? AND ? ORDER BY code, date",
            [start, as_of],
        ).df()


def run_screen(
    strategy_id: str,
    params: dict,
    as_of: date,
    store: Store,
) -> pd.DataFrame:
    cls = STRATEGY_CLASSES[strategy_id]
    strategy = cls(**params)

    listed = load_listed_info(store, as_of)
    prices = load_latest_prices(store, as_of)
    prices = prices.merge(listed[["code", "name"]], on="code", how="left")

    if strategy_id == "momentum":
        hist = load_price_history(store, as_of, days=400)
        results = strategy.screen_with_history(hist, listed, as_of)
    else:
        fin = load_fin_summary(store, as_of)
        results = strategy.screen(fin, prices, as_of)

    if not results:
        return pd.DataFrame()

    rows = []
    for r in results:
        row = {"コード": r.code, "銘柄名": r.name}
        if r.score is not None:
            row["スコア"] = round(r.score, 4)
        row.update(r.metrics)
        rows.append(row)
    return pd.DataFrame(rows)


# ── UI ────────────────────────────────────────────────────────────────────────

st.title("スクリーナー")
st.caption("本ツールの出力は学習・検証目的であり、投資助言ではありません。")

try:
    store = get_store()
except Exception as e:
    st.error(f"DB に接続できません: {e}")
    st.stop()

col_left, col_right = st.columns([1, 2])

with col_left:
    st.subheader("条件設定")

    strategy_id = st.selectbox(
        "手法",
        options=list(STRATEGY_NAMES.keys()),
        format_func=lambda k: STRATEGY_NAMES[k],
    )

    as_of = st.date_input(
        "基準日",
        value=date.today() - timedelta(days=1),
        max_value=date.today(),
        help="この日以前に開示された財務データだけを使います（ルックアヘッド防止）",
    )

    params: dict = {}
    st.markdown("**パラメータ**")

    if strategy_id == "buffett":
        params["min_roe"] = st.slider("ROE 下限", 0.05, 0.50, 0.15, 0.01, format="%.2f")
        params["max_per"] = st.slider("PER 上限（0=制限なし）", 0, 100, 25, 1)
        if params["max_per"] == 0:
            params["max_per"] = None
        params["max_debt_ratio"] = st.slider("負債比率 上限", 0.5, 5.0, 1.5, 0.1, format="%.1f")

    elif strategy_id == "graham":
        params["ncav_ratio"] = st.slider(
            "NCAV 比率（時価総額 / NCAV 上限）", 0.3, 1.0, 0.67, 0.01, format="%.2f"
        )

    elif strategy_id == "greenblatt":
        params["top_n"] = st.slider("上位銘柄数", 10, 100, 30, 5)
        params["min_market_cap"] = st.number_input(
            "時価総額下限（億円）", min_value=10, max_value=10000, value=100, step=10
        ) * 100_000_000

    elif strategy_id == "lynch":
        params["max_peg"] = st.slider("PEG 上限", 0.3, 2.0, 1.0, 0.1, format="%.1f")

    elif strategy_id == "high_dividend":
        params["min_yield"] = st.slider("配当利回り 下限", 0.01, 0.10, 0.03, 0.005, format="%.3f")
        params["max_payout"] = st.slider("配当性向 上限", 0.3, 1.0, 0.75, 0.05, format="%.2f")

    elif strategy_id == "tse_reform":
        params["max_pbr"] = st.slider("PBR 上限", 0.3, 2.0, 1.0, 0.1, format="%.1f")

    elif strategy_id == "momentum":
        params["top_n"] = st.slider("上位銘柄数", 10, 100, 30, 5)
        params["min_market_cap"] = st.number_input(
            "時価総額下限（億円）", min_value=10, max_value=10000, value=100, step=10
        ) * 100_000_000

    run_button = st.button("スクリーニング実行", type="primary", use_container_width=True)

with col_right:
    cls = STRATEGY_CLASSES[strategy_id]
    m = cls.meta
    with st.expander(f"📖 {m.name} について", expanded=False):
        st.markdown(f"**提唱者**: {m.proposer}")
        st.markdown(m.summary)
        st.markdown(f"**リバランス**: {m.rebalance_hint}")
        st.markdown(f"**弱点**: {m.weaknesses}")
        if m.report_focuses:
            st.markdown("**読み方ナビ重点箇所**")
            for f in m.report_focuses:
                st.markdown(f"- [{f.document}] **{f.section}** — {f.reason}")

    if run_button:
        with st.spinner("スクリーニング中..."):
            try:
                result_df = run_screen(
                    strategy_id, params,
                    date(as_of.year, as_of.month, as_of.day),
                    store,
                )
            except Exception as e:
                st.error(f"エラー: {e}")
                st.stop()

        if result_df.empty:
            st.info("条件に一致する銘柄が見つかりませんでした。パラメータを緩めてみてください。")
        else:
            st.success(f"{len(result_df)} 銘柄が条件に一致しました（基準日: {as_of}）")
            st.dataframe(result_df, use_container_width=True, hide_index=True)
            csv = result_df.to_csv(index=False, encoding="utf-8-sig")
            st.download_button(
                "CSV ダウンロード",
                data=csv,
                file_name=f"screen_{strategy_id}_{as_of}.csv",
                mime="text/csv",
            )
