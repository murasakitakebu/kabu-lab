"""core/data/edinet のユニットテスト（API不要の部分のみ）。"""

import zipfile
from io import BytesIO

import pytest

from core.data.edinet import DOC_TYPE_CODES, ANNUAL_REPORT_CODES, TANSHIN_CODES


def test_doc_type_codes_non_empty() -> None:
    assert len(DOC_TYPE_CODES) > 0
    for code, name in DOC_TYPE_CODES.items():
        assert len(code) == 3, f"コードは3桁のはず: {code}"
        assert name


def test_annual_report_codes_in_doc_types() -> None:
    for code in ANNUAL_REPORT_CODES:
        assert code in DOC_TYPE_CODES


def test_tanshin_codes_in_doc_types() -> None:
    for code in TANSHIN_CODES:
        assert code in DOC_TYPE_CODES


def test_no_api_key_raises() -> None:
    import os
    orig = os.environ.pop("EDINET_API_KEY", None)
    try:
        from core.data.edinet import EdinetClient
        with pytest.raises(ValueError, match="EDINET_API_KEY"):
            EdinetClient(api_key="")
    finally:
        if orig is not None:
            os.environ["EDINET_API_KEY"] = orig


def test_extract_xbrl_text_from_zip() -> None:
    """ダミー ZIP から XBRL テキストが抽出できること。"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("doc.xbrl", "<xbrl>test</xbrl>")
        zf.writestr("other.pdf", b"dummy")
    buf.seek(0)

    from core.data.edinet import EdinetClient
    # api_key を指定すれば ValueError を回避できる
    client = EdinetClient(api_key="dummy")
    result = client.extract_xbrl_text(buf.getvalue())

    assert "doc.xbrl" in result
    assert "<xbrl>test</xbrl>" in result["doc.xbrl"]
    assert "other.pdf" not in result
