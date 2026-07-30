"""Reader image-region notes interaction tests (AppTest + mocked component).

The streamlit-image-coordinates component frontend does not run inside
AppTest, so its Python callable is patched with scripted return values.
Fixtures use temporary databases and synthetic PNGs only.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
from pathlib import Path

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

import src.note_ui as note_ui
import src.runtime as runtime
from src.database import Database
from src.document_service import DocumentService
from src.evidence_basket_service import EvidenceBasketService
from src.models import NoteSourceStatus
from src.note_service import NoteNotFoundError, NoteService, NoteWriteError

READER = str(next((Path(__file__).parents[1] / "pages").glob("2_*.py")))
PNG_SIZE = (800, 1200)


def _build_reader(tmp_path: Path, monkeypatch) -> tuple[AppTest, Database, NoteService, int]:
    database = Database(tmp_path / "database" / "knowledge.db")
    raw_dir = tmp_path / "raw"
    pages_dir = tmp_path / "pages"
    raw_dir.mkdir()
    pages_dir.mkdir()
    document = database.create_document(
        title="区域界面测试",
        filename="region-ui.pdf",
        source_path=raw_dir / "region-ui.pdf",
        sha256="3" * 64,
    )
    for page_number in (1, 2):
        image_path = pages_dir / str(document.id) / f"page_{page_number:04d}.png"
        image_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", PNG_SIZE, "white").save(image_path)
        database.create_page(
            document_id=document.id,
            page_number=page_number,
            image_path=image_path,
            extracted_text=f"第 {page_number} 页 阀体",
        )
    database.update_document_page_count(document.id, 2)
    service = DocumentService(database, raw_dir, pages_dir, tmp_path / "markdown")
    monkeypatch.setattr(runtime, "application_database", lambda: database)
    monkeypatch.setattr(runtime, "application_document_service", lambda: service)
    monkeypatch.setattr(
        runtime,
        "application_evidence_basket_service",
        lambda: EvidenceBasketService(database),
    )
    note_service = NoteService(database)
    app = AppTest.from_file(READER).run(timeout=25)
    return app, database, note_service, document.id


def _drag(x1, y1, x2, y2, width=850, height=1275):
    """Scripted component return for an 850-wide display of 800x1200."""

    return {
        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
        "width": width, "height": height, "unix_time": 1,
    }


def _mock_component(monkeypatch, value_or_callable, calls: list | None = None):
    def fake(source, **kwargs):
        if calls is not None:
            calls.append(kwargs)
        if callable(value_or_callable):
            return value_or_callable(kwargs)
        return value_or_callable

    monkeypatch.setattr(note_ui, "streamlit_image_coordinates", fake)


def _button(app: AppTest, key: str):
    matches = [button for button in app.button if button.key == key]
    assert matches, f"找不到按钮 {key}"
    return matches[0]


def _warnings(app: AppTest) -> list[str]:
    return [warning.value for warning in app.warning]


def _errors(app: AppTest) -> list[str]:
    return [error.value for error in app.error]


def _captions(app: AppTest) -> list[str]:
    return [caption.value for caption in app.caption]



def _create_region(app: AppTest, page_id: int):
    key = f"note_image_create_region_{page_id}"
    return app.session_state[key] if key in app.session_state else None

def _page1(database: Database, document_id: int):
    return database.get_page_by_number(document_id, 1)


def _png_hash(database: Database, document_id: int) -> str:
    page = _page1(database, document_id)
    return hashlib.sha256(Path(page.image_path).read_bytes()).hexdigest()


# --- A. dependency & component contract ---------------------------------------


def test_component_version_and_call_contract(tmp_path: Path, monkeypatch) -> None:
    assert importlib.metadata.version("streamlit-image-coordinates") == "0.4.0"
    calls: list = []
    _mock_component(monkeypatch, None, calls)
    app, _, _, _ = _build_reader(tmp_path, monkeypatch)
    assert not app.exception
    assert calls, "创建组件应被调用"
    for kwargs in calls:
        assert kwargs["click_and_drag"] is True
        assert kwargs["cursor"] == "crosshair"
        assert "_w" in kwargs["key"]
    assert any("create_region" in kwargs["key"] for kwargs in calls)
    assert "streamlit_drawable_canvas" not in dir(note_ui)


def test_component_key_contains_display_width(tmp_path: Path, monkeypatch) -> None:
    calls: list = []
    _mock_component(monkeypatch, None, calls)
    app, _, _, _ = _build_reader(tmp_path, monkeypatch)
    key = next(kwargs["key"] for kwargs in calls if "create_region" in kwargs["key"])
    assert key.endswith("_w850")
    slider = next(s for s in app.slider if s.label == "页面缩放")
    slider.set_value(600).run()
    calls.clear()
    app.run(timeout=25)
    key = next(kwargs["key"] for kwargs in calls if "create_region" in kwargs["key"])
    assert key.endswith("_w600")


# --- B. drag handling ------------------------------------------------------------


def test_valid_drag_stores_region_in_session(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, _drag(85, 128, 340, 510))
    app, _, _, _ = _build_reader(tmp_path, monkeypatch)
    assert not app.exception
    successes = [success.value for success in app.success]
    assert any("当前框选" in value for value in successes)


def test_reversed_drag_sorted(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, _drag(340, 510, 85, 128))
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    assert _create_region(app, page.id) == {"x0": 80, "y0": 120, "x1": 320, "y1": 480}


def test_out_of_bounds_clamped(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, _drag(-100, -100, 2000, 3000))
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    assert _create_region(app, page.id) == {"x0": 0, "y0": 0, "x1": 800, "y1": 1200}


def test_zero_area_and_garbage_rejected(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, _drag(100, 100, 100, 100))
    app, database, _, document_id = _build_reader(tmp_path, monkeypatch)
    assert any("框选区域无效" in value for value in _warnings(app))
    page = _page1(database, document_id)
    assert _create_region(app, page.id) is None

    _mock_component(monkeypatch, {"x1": None, "y1": 1})
    app.run(timeout=25)
    assert any("框选区域无效" in value for value in _warnings(app))


def test_rerun_does_not_duplicate_create(tmp_path: Path, monkeypatch) -> None:
    _mock_component(
        monkeypatch,
        lambda kwargs: _drag(85, 128, 340, 510) if "_v0_" in kwargs["key"] else None,
    )
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    app.text_area(key=f"note_image_create_personal_{page.id}").input("区域笔记").run()
    _button(app, f"note_image_create_save_{page.id}").click().run()
    app.run(timeout=25)
    app.run(timeout=25)
    notes = note_service.list_page_notes(page.id)
    assert len(notes) == 1


# --- C. zoom consistency ----------------------------------------------------------


def test_same_region_maps_identically_across_widths(tmp_path: Path, monkeypatch) -> None:
    results: dict[int, dict] = {}
    for width in (500, 850, 1400):
        scale = width / 800
        _mock_component(
            monkeypatch,
            _drag(int(80 * scale), int(120 * scale), int(320 * scale), int(480 * scale),
                  width=width, height=int(1200 * scale)),
        )
        app, database, _, document_id = _build_reader(tmp_path / f"w{width}", monkeypatch)
        slider = next(s for s in app.slider if s.label == "页面缩放")
        slider.set_value(width).run()
        page = _page1(database, document_id)
        results[width] = _create_region(app, page.id)
    assert results[500] == results[850] == results[1400] == {
        "x0": 80, "y0": 120, "x1": 320, "y1": 480
    }


# --- D. create success --------------------------------------------------------------


def test_create_success_uses_service_side_png_facts(tmp_path: Path, monkeypatch) -> None:
    _mock_component(
        monkeypatch,
        lambda kwargs: _drag(85, 128, 340, 510) if "_v0_" in kwargs["key"] else None,
    )
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    expected_hash = _png_hash(database, document_id)

    app.text_area(key=f"note_image_create_personal_{page.id}").input("阀体区域").run()
    _button(app, f"note_image_create_save_{page.id}").click().run()
    assert not app.exception

    views = note_service.list_page_notes(page.id)
    assert len(views) == 1
    note = views[0].note
    assert note.personal_note == "阀体区域"
    assert note.region_image_width == 800 and note.region_image_height == 1200
    assert note.region_image_sha256 == expected_hash
    assert (note.region_x0, note.region_y0, note.region_x1, note.region_y1) == (
        80, 120, 320, 480,
    )
    assert views[0].source_status is NoteSourceStatus.VALID
    # 状态清理：框选与个人笔记清空、组件版本前进
    assert f"note_image_create_region_{page.id}" not in app.session_state
    assert app.text_area(key=f"note_image_create_personal_{page.id}").value == ""
    assert any("图片区域笔记已保存" in success.value for success in app.success)


# --- E. create failures ----------------------------------------------------------------


def test_create_requires_region_and_personal(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, None)
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _button(app, f"note_image_create_save_{page.id}").click().run()
    assert any("请先在页面图像上拖拽" in value for value in _warnings(app))

    _mock_component(monkeypatch, _drag(85, 128, 340, 510))
    app.run(timeout=25)
    _button(app, f"note_image_create_save_{page.id}").click().run()
    assert "个人笔记不能为空" in _warnings(app)
    assert note_service.list_page_notes(page.id) == []


def test_create_missing_and_unreadable_png(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, None)
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    Path(page.image_path).unlink()
    app.run(timeout=25)
    assert any("当前页面图像不存在" in value for value in _warnings(app))

    # 损坏 PNG：阅读页自身 st.image 直接异常（既有行为，不属于本任务范围），
    # 服务层 PageImageUnreadableError 路径由服务层测试覆盖。


def test_create_failure_keeps_inputs(tmp_path: Path, monkeypatch) -> None:
    _mock_component(
        monkeypatch,
        lambda kwargs: _drag(85, 128, 340, 510) if "_v0_" in kwargs["key"] else None,
    )
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)

    def fail(*args, **kwargs):
        raise NoteWriteError("保存笔记失败，请重试")

    monkeypatch.setattr(NoteService, "create_image_region_note", fail)
    app.text_area(key=f"note_image_create_personal_{page.id}").input("别丢").run()
    _button(app, f"note_image_create_save_{page.id}").click().run()
    assert any("保存失败" in value for value in _errors(app))
    assert app.text_area(key=f"note_image_create_personal_{page.id}").value == "别丢"
    assert f"note_image_create_region_{page.id}" in app.session_state


# --- F. display & status ------------------------------------------------------------------


def _seed_region(note_service: NoteService, page_id: int) -> int:
    return note_service.create_image_region_note(page_id, 10, 20, 300, 400, "区域一").note.id


def test_display_and_valid_status(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, None)
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _seed_region(note_service, page.id)
    app.run(timeout=25)
    captions = _captions(app)
    assert any("区域：(10, 20) - (300, 400)" in value for value in captions)
    assert any("区域 290 × 380" in value for value in captions)
    assert any("创建时图像 800 × 1200" in value for value in captions)
    assert any("图片来源有效" in value for value in captions)


def test_changed_missing_unreadable_status(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, None)
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _seed_region(note_service, page.id)

    Image.new("RGB", PNG_SIZE, "blue").save(page.image_path)  # 同尺寸内容变化
    app.run(timeout=25)
    assert any("已经变化" in value for value in _warnings(app))

    Path(page.image_path).unlink()
    app.run(timeout=25)
    assert any("页面图像已经不存在" in value for value in _warnings(app))
    # 损坏 PNG 的 unreadable 状态在服务层测试覆盖；阅读页自身 st.image 对损坏
    # 文件会直接异常（既有行为，不属于本任务范围）。


# --- G. edit personal ---------------------------------------------------------------------


def test_edit_personal_preserves_anchor(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, None)
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_region(note_service, page.id)
    app.run(timeout=25)

    _button(app, f"note_image_edit_open_{note_id}").click().run()
    app.text_area(key=f"note_image_edit_personal_{note_id}").input("修订后的笔记").run()
    assert any("有未保存修改" in value for value in _warnings(app))
    _button(app, f"note_image_edit_save_{note_id}").click().run()

    updated = note_service.get_note(note_id).note
    assert updated.personal_note == "修订后的笔记"
    assert (updated.region_x0, updated.region_y0) == (10, 20)
    assert updated.region_image_sha256 is not None


# --- H. rebind -------------------------------------------------------------------------------


def test_rebind_full_flow(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_region(note_service, page.id)
    before = note_service.get_note(note_id).note

    def component(kwargs):
        if f"rebind_region_{note_id}" in kwargs["key"] and "_v0_" in kwargs["key"]:
            return _drag(170, 255, 510, 680)
        return None

    _mock_component(monkeypatch, component)
    app.run(timeout=25)

    # 未激活重框时不渲染重框组件 key
    _button(app, f"note_image_rebind_start_{note_id}").click().run()
    infos = [info.value for info in app.info]
    assert any("新区域" in value for value in infos)
    apply_button = _button(app, f"note_image_rebind_apply_{note_id}")
    assert apply_button.disabled

    app.checkbox(key=f"note_image_rebind_confirm_{note_id}").check().run()
    assert not _button(app, f"note_image_rebind_apply_{note_id}").disabled
    _button(app, f"note_image_rebind_apply_{note_id}").click().run()

    rebound = note_service.get_note(note_id).note
    assert rebound.id == before.id
    assert rebound.created_at == before.created_at
    assert rebound.personal_note == "区域一"
    assert (rebound.region_x0, rebound.region_y0, rebound.region_x1, rebound.region_y1) == (
        160, 240, 480, 640,
    )
    assert rebound.region_image_sha256 == _png_hash(database, document_id)
    rebind_region_key = f"note_image_rebind_region_{note_id}"
    assert rebind_region_key not in app.session_state


def test_rebind_failure_keeps_original(tmp_path: Path, monkeypatch) -> None:
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_region(note_service, page.id)
    before = note_service.get_note(note_id).note

    def component(kwargs):
        if f"rebind_region_{note_id}" in kwargs["key"] and "_v0_" in kwargs["key"]:
            return _drag(170, 255, 510, 680)
        return None

    _mock_component(monkeypatch, component)
    app.run(timeout=25)
    _button(app, f"note_image_rebind_start_{note_id}").click().run()
    app.checkbox(key=f"note_image_rebind_confirm_{note_id}").check().run()
    monkeypatch.setattr(
        NoteService,
        "rebind_image_region",
        lambda *args, **kwargs: (_ for _ in ()).throw(NoteWriteError("更新笔记失败")),
    )
    _button(app, f"note_image_rebind_apply_{note_id}").click().run()
    assert any("保存失败" in value for value in _errors(app))
    assert note_service.get_note(note_id).note == before


# --- I. delete ---------------------------------------------------------------------------------


def test_delete_region_note_keeps_png_and_others(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, None)
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    note_id = _seed_region(note_service, page.id)
    other = note_service.create_page_note(page.id, "页面级保留").note.id
    before_hash = _png_hash(database, document_id)
    app.run(timeout=25)

    delete_button = _button(app, f"note_delete_{note_id}")
    assert delete_button.disabled
    app.checkbox(key=f"note_delete_confirm_{note_id}").check().run()
    _button(app, f"note_delete_{note_id}").click().run()

    with pytest.raises(NoteNotFoundError):
        note_service.get_note(note_id)
    assert _png_hash(database, document_id) == before_hash
    assert note_service.get_note(other).note.personal_note == "页面级保留"


# --- K. query & hashing discipline --------------------------------------------------------------


def test_queries_and_png_hashing_are_bounded(tmp_path: Path, monkeypatch) -> None:
    calls = {"list_document_notes": 0, "list_page_notes": 0, "get_note": 0, "read_png": 0}
    original_doc = NoteService.list_document_notes
    original_page = NoteService.list_page_notes
    original_get = NoteService.get_note
    original_read = NoteService._read_page_image

    def counted(name, original):
        def wrapper(*args, **kwargs):
            calls[name] += 1
            return original(*args, **kwargs)

        return wrapper

    def counted_read(self, path):
        calls["read_png"] += 1
        return original_read(self, path)

    monkeypatch.setattr(
        NoteService, "list_document_notes", counted("list_document_notes", original_doc)
    )
    monkeypatch.setattr(NoteService, "list_page_notes", counted("list_page_notes", original_page))
    monkeypatch.setattr(NoteService, "get_note", counted("get_note", original_get))
    monkeypatch.setattr(NoteService, "_read_page_image", counted_read)

    _mock_component(monkeypatch, None)
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    _seed_region(note_service, page.id)
    note_service.create_image_region_note(page.id, 50, 60, 200, 300, "区域二")
    for key in calls:
        calls[key] = 0
    app.run(timeout=25)
    assert not app.exception
    assert calls["list_document_notes"] == 1
    assert calls["list_page_notes"] == 1
    assert calls["get_note"] == 0
    # 同页两条区域笔记：状态检查共享一次读取；另有创建区预览一次
    assert calls["read_png"] == 2


# --- 状态隔离 ---------------------------------------------------------------------------------


def test_region_does_not_leak_across_pages(tmp_path: Path, monkeypatch) -> None:
    _mock_component(
        monkeypatch,
        lambda kwargs: _drag(85, 128, 340, 510)
        if "_pg1_" in kwargs["key"] and "_v0_" in kwargs["key"]
        else None,
    )
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page2 = database.get_page_by_number(document_id, 2)
    page1 = _page1(database, document_id)
    assert f"note_image_create_region_{page1.id}" in app.session_state

    page_select = next(sb for sb in app.selectbox if sb.label == "页码")
    page_select.select("2").run()
    assert _create_region(app, page2.id) is None


def test_advanced_manual_coordinates(tmp_path: Path, monkeypatch) -> None:
    _mock_component(monkeypatch, None)
    app, database, note_service, document_id = _build_reader(tmp_path, monkeypatch)
    page = _page1(database, document_id)
    app.number_input(key=f"note_image_manual_x0_{page.id}").set_value(50).run()
    app.number_input(key=f"note_image_manual_y0_{page.id}").set_value(60).run()
    app.number_input(key=f"note_image_manual_x1_{page.id}").set_value(250).run()
    app.number_input(key=f"note_image_manual_y1_{page.id}").set_value(360).run()
    _button(app, f"note_image_manual_apply_{page.id}").click().run()
    assert _create_region(app, page.id) == {"x0": 50, "y0": 60, "x1": 250, "y1": 360}

    app.text_area(key=f"note_image_create_personal_{page.id}").input("高级坐标笔记").run()
    _button(app, f"note_image_create_save_{page.id}").click().run()
    note = note_service.list_page_notes(page.id)[0].note
    assert (note.region_x0, note.region_y0, note.region_x1, note.region_y1) == (
        50, 60, 250, 360,
    )
