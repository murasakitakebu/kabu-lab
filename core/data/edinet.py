"""EDINET API v2 クライアント。

Subscription Key 認証を使い、書類一覧・書類取得を行う。
レートリミット：公式非公開のため保守的に 10 req/秒 以内に制限。
"""

from __future__ import annotations

import logging
import os
import time
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

EDINET_BASE = "https://disclosure.edinet-fsa.go.jp/api/v2"
_MIN_INTERVAL = 0.15  # 秒（約6 req/秒）


@dataclass
class DocInfo:
    """書類情報のサマリー。"""

    doc_id: str
    edinet_code: str
    sec_code: str | None  # 証券コード（上場企業のみ）
    company_name: str
    doc_type_code: str
    doc_description: str
    period_start: str | None
    period_end: str | None
    submit_date: str
    pdf_flag: bool
    xbrl_flag: bool


class EdinetClient:
    """EDINET API v2 の薄いラッパー。"""

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("EDINET_API_KEY", "")
        if not self._api_key:
            raise ValueError(
                "EDINET_API_KEY が設定されていません。.env ファイルを確認してください。"
            )
        self._session = requests.Session()
        self._last_request_at: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - elapsed)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> requests.Response:
        self._throttle()
        url = f"{EDINET_BASE}/{path}"
        p = {"Subscription-Key": self._api_key, **(params or {})}
        resp = self._session.get(url, params=p, timeout=30)
        self._last_request_at = time.monotonic()
        resp.raise_for_status()
        return resp

    def get_doc_list(self, target_date: date, doc_type: int = 2) -> list[DocInfo]:
        """指定日の書類一覧を取得する。

        Parameters
        ----------
        target_date:
            取得対象日
        doc_type:
            1 = 一覧のみ（メタ）、2 = 一覧＋提出者情報（デフォルト）
        """
        resp = self._get(
            "documents.json",
            {"date": target_date.isoformat(), "type": doc_type},
        )
        data = resp.json()
        results: list[DocInfo] = []
        for d in data.get("results", []):
            results.append(
                DocInfo(
                    doc_id=d.get("docID", ""),
                    edinet_code=d.get("edinetCode", ""),
                    sec_code=d.get("secCode") or None,
                    company_name=d.get("filerName", ""),
                    doc_type_code=d.get("docTypeCode", ""),
                    doc_description=d.get("docDescription", ""),
                    period_start=d.get("periodStart") or None,
                    period_end=d.get("periodEnd") or None,
                    submit_date=d.get("submitDateTime", "")[:10],
                    pdf_flag=bool(d.get("pdfFlag")),
                    xbrl_flag=bool(d.get("xbrlFlag")),
                )
            )
        return results

    def get_doc_list_range(
        self,
        start: date,
        end: date,
        doc_type_codes: list[str] | None = None,
    ) -> list[DocInfo]:
        """期間中の書類一覧を日次で取得して結合する。"""
        all_docs: list[DocInfo] = []
        current = start
        while current <= end:
            try:
                docs = self.get_doc_list(current)
                if doc_type_codes:
                    docs = [d for d in docs if d.doc_type_code in doc_type_codes]
                all_docs.extend(docs)
            except Exception as exc:
                logger.warning("get_doc_list failed for %s: %s", current, exc)
            current += timedelta(days=1)
        return all_docs

    def get_doc_list_by_sec_code(
        self,
        sec_code: str,
        start: date,
        end: date,
        doc_type_codes: list[str] | None = None,
    ) -> list[DocInfo]:
        """特定の証券コードに絞って書類一覧を返す。"""
        # J-Quants の 5 桁コードを 4 桁に変換（EDINET は 4 桁 + 0）
        # EDINET の secCode は "72030" のような5桁または "7203" の4桁
        all_docs = self.get_doc_list_range(start, end, doc_type_codes)
        return [d for d in all_docs if (d.sec_code or "").startswith(sec_code[:4])]

    def get_pdf(self, doc_id: str) -> bytes:
        """書類の PDF を取得する（バイト列で返す）。"""
        resp = self._get(f"documents/{doc_id}", {"type": 2})
        return resp.content

    def get_xbrl_zip(self, doc_id: str) -> bytes:
        """書類の XBRL ZIP を取得する（バイト列で返す）。"""
        resp = self._get(f"documents/{doc_id}", {"type": 1})
        return resp.content

    def extract_xbrl_text(self, xbrl_zip_bytes: bytes) -> dict[str, str]:
        """XBRL ZIP から .xbrl ファイルのテキストを取り出す。

        Returns
        -------
        dict:
            {ファイル名: テキスト内容}
        """
        result: dict[str, str] = {}
        with zipfile.ZipFile(BytesIO(xbrl_zip_bytes)) as zf:
            for name in zf.namelist():
                if name.lower().endswith(".xbrl"):
                    try:
                        content = zf.read(name).decode("utf-8", errors="replace")
                        result[name] = content
                    except Exception as exc:
                        logger.warning("Failed to read %s: %s", name, exc)
        return result

    def save_pdf(self, doc_id: str, dest: Path) -> Path:
        """PDF を指定パスに保存する。"""
        content = self.get_pdf(doc_id)
        dest.write_bytes(content)
        logger.info("Saved PDF: %s (%d bytes)", dest, len(content))
        return dest


# 書類種別コードの主要な定義（参考）
DOC_TYPE_CODES: dict[str, str] = {
    "010": "有価証券報告書",
    "030": "四半期報告書",
    "043": "半期報告書",
    "120": "決算短信（日本基準）",
    "130": "決算短信（米国基準）",
    "140": "決算短信（IFRS）",
    "160": "四半期決算短信（日本基準）",
    "170": "四半期決算短信（米国基準）",
    "180": "四半期決算短信（IFRS）",
}

# 有報・短信のコード一覧
ANNUAL_REPORT_CODES = ["010"]
QUARTERLY_REPORT_CODES = ["030", "043"]
TANSHIN_CODES = ["120", "130", "140", "160", "170", "180"]
