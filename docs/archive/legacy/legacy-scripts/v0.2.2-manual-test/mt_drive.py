"""MT-02..MT-09 driver for the isolated v0.2.2 batch-2 manual OCR test.

Runs against the isolated data root via EKB_* env vars, using Streamlit
AppTest on the real review page with the real RapidOcrEngine. Read-only
with respect to the repository.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from streamlit.testing.v1 import AppTest

import src.runtime as runtime
from src.evidence_basket_service import EvidenceBasketService
from src.evidence_service import OCR_EVIDENCE_WARNING
from src.models import PageStatus, SearchField
from src.search_service import SearchService

ROOT = Path(r"D:\ekb-v0.2.2-manual-test")
PDF = ROOT / "ocr-manual-test.pdf"
DB = ROOT / "data" / "database" / "knowledge.db"
REPORT: dict[str, object] = {}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review_app(page_id: int, timeout: int = 180) -> AppTest:
    app = AppTest.from_file(str(Path("pages/4_待整理页面.py").resolve()))
    app.query_params["page_id"] = str(page_id)
    app.run(timeout=timeout)
    return app


# --- MT-02 import -------------------------------------------------------------
service = runtime.application_document_service()
database = runtime.application_database()
pdf_hash_before = sha256(PDF)
result = service.import_pdf(PDF.read_bytes(), PDF.name, title="OCR人工测试")
doc_id = result.document.id
pages = [p for p in database.list_pages(document_id=doc_id)]
assert len(pages) == 3, f"expected 3 pages, got {len(pages)}"
p1, p2, p3 = pages
png_hash = {p.id: sha256(p.image_path) for p in pages}
REPORT["MT-02"] = {
    "doc_id": doc_id,
    "pages": [(p.id, p.page_number, p.status.value) for p in pages],
    "pdf_sha256_before": pdf_hash_before,
    "png_sha256": png_hash,
    "page1_extracted_len": len(p1.extracted_text or ""),
    "page3_extracted_len": len(p3.extracted_text or ""),
}
print("MT-02 import OK:", REPORT["MT-02"]["pages"])

# wrap the real engine to count actual recognize() calls
engine = runtime.application_ocr_engine()
calls: list[Path] = []
_orig = engine.recognize
def counting_recognize(image_path: Path) -> str:
    calls.append(image_path)
    return _orig(image_path)
engine.recognize = counting_recognize  # type: ignore[method-assign]

# --- MT-03 page 1 English/digits OCR ------------------------------------------
app = review_app(p1.id)
assert not app.exception, app.exception
btn = [b for b in app.button if b.label == "执行本地 OCR"]
assert len(btn) == 1, f"expected 1 OCR button, got {[b.label for b in app.button]}"
assert btn[0].key == f"review_run_ocr_{p1.id}"
md_before = database.get_page(p1.id).markdown_content
t0 = time.perf_counter()
btn[0].click().run(timeout=300)
first_ocr_seconds = round(time.perf_counter() - t0, 2)
assert not app.exception, app.exception
page1 = database.get_page(p1.id)
REPORT["MT-03"] = {
    "first_ocr_seconds": first_ocr_seconds,
    "success_shown": any(i.value == "本页 OCR 已完成。" for i in app.success),
    "draft_heading": any("未经人工核验" in m.value for m in app.markdown),
    "draft_disabled": app.text_area(key=f"review_ocr_draft_{p1.id}").disabled,
    "ocr_text": page1.ocr_text,
    "processing_status": page1.processing_status,
    "markdown_unchanged": page1.markdown_content == md_before,
    "status": page1.status.value,
    "png_hash_unchanged": sha256(p1.image_path) == png_hash[p1.id],
    "button_gone_after": "执行本地 OCR" not in {b.label for b in app.button},
    "engine_calls": len(calls),
}
compact1 = page1.ocr_text.replace(" ", "").upper()
assert "STM32" in compact1 and "ADC" in compact1, page1.ocr_text
print("MT-03 OK:", json.dumps({k: v for k, v in REPORT["MT-03"].items() if k != "ocr_text"}, ensure_ascii=False))
print("  page1 ocr_text:", repr(page1.ocr_text))

# --- MT-04 page 2 Chinese OCR ---------------------------------------------------
app = review_app(p2.id)
assert not app.exception, app.exception
btn = [b for b in app.button if b.label == "执行本地 OCR"]
assert len(btn) == 1
btn[0].click().run(timeout=300)
assert not app.exception, app.exception
page2 = database.get_page(p2.id)
page1_after = database.get_page(p1.id)
compact2 = page2.ocr_text.replace(" ", "")
REPORT["MT-04"] = {
    "ocr_text": page2.ocr_text,
    "chinese_markers": {k: (k in compact2) for k in ("本地", "OCR", "测试")},
    "page1_unchanged": page1_after.ocr_text == page1.ocr_text,
    "png_hash_unchanged": sha256(p2.image_path) == png_hash[p2.id],
    "engine_calls": len(calls),
}
print("MT-04:", json.dumps(REPORT["MT-04"], ensure_ascii=False))

# --- MT-05 page 3 reliable text layer ------------------------------------------
calls_before = len(calls)
app = review_app(p3.id)
assert not app.exception, app.exception
labels = {b.label for b in app.button}
clicked = False
info_texts = [i.value for i in app.info]
if "执行本地 OCR" in labels:
    next(b for b in app.button if b.label == "执行本地 OCR").click().run(timeout=60)
    assert not app.exception, app.exception
    info_texts = [i.value for i in app.info]
    clicked = True
page3 = database.get_page(p3.id)
REPORT["MT-05"] = {
    "button_present": "执行本地 OCR" in labels,
    "clicked": clicked,
    "info": info_texts,
    "engine_called": len(calls) > calls_before,
    "ocr_text": page3.ocr_text,
    "extracted_unchanged": len(page3.extracted_text or "") >= 20,
    "processing_status": page3.processing_status,
}
print("MT-05:", json.dumps(REPORT["MT-05"], ensure_ascii=False))

# --- MT-06 search ----------------------------------------------------------------
search = SearchService(database)
r_en = search.search("STM32")
zh_keyword = "本地" if "本地" in compact2 else ("测试" if "测试" in compact2 else None)
r_zh = search.search(zh_keyword) if zh_keyword else []
REPORT["MT-06"] = {
    "stm32_hits": [(r.page_id, [f.value for f in r.match_fields]) for r in r_en],
    "zh_keyword": zh_keyword,
    "zh_hits": [(r.page_id, [f.value for f in r.match_fields]) for r in r_zh],
}
print("MT-06:", json.dumps(REPORT["MT-06"], ensure_ascii=False))

# --- MT-07 evidence basket ---------------------------------------------------------
basket = EvidenceBasketService(database)
basket.add_item(document_id=doc_id, page_id=p1.id, evidence_text=page1.ocr_text.strip())
export = basket.export_markdown()
REPORT["MT-07"] = {
    "warning_present": OCR_EVIDENCE_WARNING in export,
    "has_page_ref": f"第 {p1.page_number} 页" in export or f"page {p1.page_number}" in export.lower(),
    "abs_path_leak": ("D:\\" in export or "D:/" in export),
    "export_excerpt": export[:400],
}
print("MT-07:", json.dumps({k: v for k, v in REPORT["MT-07"].items() if k != "export_excerpt"}, ensure_ascii=False))

# --- MT-08 data protection (read-only sqlite) ---------------------------------------
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute(
    "SELECT id, page_number, length(COALESCE(extracted_text,'')), length(COALESCE(ocr_text,'')), "
    "length(COALESCE(markdown_content,'')), status, processing_status, COALESCE(processing_error,'') "
    "FROM pages WHERE document_id=? ORDER BY page_number", (doc_id,),
).fetchall()
con.close()
REPORT["MT-08"] = {
    "rows": rows,
    "pdf_sha256_after": sha256(PDF),
    "png_sha256_after": {pid: sha256(p.image_path) for pid, p in ((p1.id, p1), (p2.id, p2), (p3.id, p3))},
}
print("MT-08:", json.dumps(REPORT["MT-08"], ensure_ascii=False, default=str))

# --- MT-09 repeat protection ----------------------------------------------------------
calls_before = len(calls)
app = review_app(p1.id)
assert not app.exception, app.exception
REPORT["MT-09"] = {
    "button_present": "执行本地 OCR" in {b.label for b in app.button},
    "draft_shown": app.text_area(key=f"review_ocr_draft_{p1.id}").value == page1.ocr_text,
    "engine_calls_added": len(calls) - calls_before,
    "ocr_text_unchanged": database.get_page(p1.id).ocr_text == page1.ocr_text,
}
print("MT-09:", json.dumps(REPORT["MT-09"], ensure_ascii=False))

REPORT["pdf_sha256_final"] = sha256(PDF)
out = ROOT / "mt-results.json"
out.write_text(json.dumps(REPORT, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
print("REPORT WRITTEN:", out)
