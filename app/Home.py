"""Kabu Lab トップページ。"""

import streamlit as st

st.set_page_config(page_title="Kabu Lab", layout="wide")

st.title("Kabu Lab")
st.caption("日本株 戦略研究プラットフォーム")

st.warning("本ツールの出力は学習・検証目的であり、投資助言ではありません。")

st.markdown(
    """
左のメニューから機能を選んでください。

- **タイムマシン計算機**：過去のある日に投資していたら今いくらか（Phase 1）
"""
)
