"""Generate deterministic synthetic PDFs for v0.2.3 scale testing.

A-level synthetic material: generated at runtime, never committed to Git, and
never written into the formal data directory ``D:/Projects/engineering-kb``.
Page text is a pure function of the document id, page number and text-length
target, so identical arguments produce identical page text across runs.

CLI example::

    python scripts/generate_scale_pdf.py --output runtime/v023-scale/pdfs/mix-50.pdf \
        --pages 50 --special-preset --overwrite
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pymupdf

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
FORMAL_PROJECT_ROOT: Final[Path] = Path(r"D:\Projects\engineering-kb")

PAGE_WIDTH: Final[float] = 595
PAGE_HEIGHT: Final[float] = 842
WIDE_PAGE_WIDTH: Final[float] = 842
WIDE_PAGE_HEIGHT: Final[float] = 595
DEFAULT_TEXT_LENGTH: Final[int] = 200
SHORT_PAGE_TEXT: Final[str] = "ab 12"
_FIXED_METADATA_DATE: Final[str] = "D:20260101000000Z"

SPECIAL_PAGE_TYPES: Final[tuple[str, ...]] = (
    "blank",
    "short",
    "rot90",
    "rot180",
    "rot270",
    "wide",
    "image",
)
_ROTATION_BY_TYPE: Final[dict[str, int]] = {"rot90": 90, "rot180": 180, "rot270": 270}

# Fixed filler vocabulary: the filler text of a page only depends on the page
# number and the remaining length budget, never on random state.
_FILLER_WORDS: Final[tuple[str, ...]] = (
    "hydraulic", "pressure", "torque", "bearing", "flange", "gasket", "valve",
    "sensor", "circuit", "voltage", "current", "weld", "fatigue", "tolerance",
    "caliper", "lubricant", "piston", "nozzle", "turbine", "gearbox",
    "conduit", "insulation", "fastener", "bracket", "housing", "impeller",
    "coupling", "sealant", "duct", "reservoir", "actuator", "manifold",
)


class FormalPathError(ValueError):
    """Raised when an output path points into the formal data directory."""


@dataclass(frozen=True, slots=True)
class ScalePdfResult:
    """Facts about one generated scale-test PDF."""

    path: Path
    pages: int
    document_id: str
    size_bytes: int
    sha256: str
    duration_seconds: float


def page_token(document_id: str, page_number: int) -> str:
    """Return the unique searchable header line of a normal page."""

    return (
        f"SCALE {document_id} PAGE {page_number} "
        f"TOKEN {document_id}-{page_number:06d}"
    )


def normal_page_text(
    document_id: str,
    page_number: int,
    *,
    text_length: int = DEFAULT_TEXT_LENGTH,
    unique_text: bool = True,
) -> str:
    """Return the deterministic text layer of one normal page."""

    header = (
        page_token(document_id, page_number)
        if unique_text
        else f"SCALE {document_id} PAGE {page_number}"
    )
    filler = _filler_text(page_number, max(0, text_length - len(header) - 1))
    return f"{header}\n{filler}" if filler else header


def derive_document_id(output_path: Path) -> str:
    """Derive a stable document id from the output file name."""

    slug = re.sub(r"[^A-Za-z0-9]+", "-", output_path.stem).strip("-").upper()
    return slug or "SCALE-DOC"


def parse_special_pages(spec: str) -> dict[int, str]:
    """Parse ``blank:7,short:8`` into ``{7: "blank", 8: "short"}``."""

    mapping: dict[int, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        kind, separator, raw_number = chunk.partition(":")
        kind = kind.strip().lower()
        if not separator or not raw_number.strip():
            raise ValueError(f"无效的特殊页条目：{chunk!r}（应为 类型:页号）")
        if kind not in SPECIAL_PAGE_TYPES:
            allowed = ", ".join(SPECIAL_PAGE_TYPES)
            raise ValueError(f"未知的特殊页类型：{kind!r}（可选：{allowed}）")
        try:
            number = int(raw_number.strip())
        except ValueError:
            raise ValueError(f"特殊页页号必须是正整数：{chunk!r}") from None
        if number < 1:
            raise ValueError(f"特殊页页号必须从 1 开始：{chunk!r}")
        if number in mapping:
            raise ValueError(f"特殊页页号重复：第 {number} 页")
        mapping[number] = kind
    return mapping


def preset_special_pages(page_count: int) -> dict[int, str]:
    """Return a fixed mixed preset with one page of each special type.

    Positions spread evenly across the document so a 50-page smoke document
    exercises every type without manual ``--special-pages`` strings.
    """

    mapping: dict[int, str] = {}
    total = len(SPECIAL_PAGE_TYPES)
    for index, kind in enumerate(SPECIAL_PAGE_TYPES):
        candidate = 1 + round((page_count - 1) * (2 * index + 1) / (2 * total))
        # Bounded search: with fewer pages than special types there may be no
        # free slot left; skip that type instead of cycling forever.
        for _ in range(page_count):
            if candidate not in mapping:
                mapping[candidate] = kind
                break
            candidate += 1
            if candidate > page_count:
                candidate = 1
    return mapping


def build_scale_pdf(
    output: Path | str,
    *,
    pages: int,
    document_id: str | None = None,
    text_length: int = DEFAULT_TEXT_LENGTH,
    unique_text: bool = True,
    special_pages: dict[int, str] | None = None,
    overwrite: bool = False,
) -> ScalePdfResult:
    """Build one deterministic scale-test PDF page by page.

    Pages are rendered one at a time and appended to the document; page text
    is never accumulated into a process-wide list.  Existing files are kept
    unless ``overwrite`` is true, and paths inside the formal data directory
    are always rejected.
    """

    output_path = Path(output)
    _reject_formal_path(output_path)
    if pages < 1:
        raise ValueError(f"页数必须是正整数：{pages}")
    if text_length < 0:
        raise ValueError(f"文本长度不能为负数：{text_length}")
    if output_path.exists() and not overwrite:
        raise FileExistsError(f"输出文件已存在（未指定 --overwrite）：{output_path}")
    resolved_document_id = (document_id or derive_document_id(output_path)).strip()
    if not resolved_document_id:
        raise ValueError("文档标识不能为空")
    specials = dict(special_pages or {})
    for page_number, kind in specials.items():
        if kind not in SPECIAL_PAGE_TYPES:
            raise ValueError(f"未知的特殊页类型：{kind!r}")
        if page_number < 1 or page_number > pages:
            raise ValueError(f"特殊页 {kind}:{page_number} 超出总页数 {pages}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    document = pymupdf.open()
    try:
        document.set_metadata(
            {
                "producer": "ekb-scale-generator",
                "creator": "scripts/generate_scale_pdf.py",
                "title": resolved_document_id,
                "creationDate": _FIXED_METADATA_DATE,
                "modDate": _FIXED_METADATA_DATE,
            }
        )
        for page_number in range(1, pages + 1):
            _add_page(
                document,
                resolved_document_id,
                page_number,
                text_length=text_length,
                unique_text=unique_text,
                special=specials.get(page_number),
            )
        document.save(str(output_path), garbage=3, deflate=True)
    finally:
        document.close()
    duration = time.perf_counter() - started
    return ScalePdfResult(
        path=output_path,
        pages=pages,
        document_id=resolved_document_id,
        size_bytes=output_path.stat().st_size,
        sha256=_sha256_of_file(output_path),
        duration_seconds=duration,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser for the scale PDF generator."""

    parser = argparse.ArgumentParser(
        description="生成 v0.2.3 容量测试用的确定性合成 PDF（A 级测试资料）。",
    )
    parser.add_argument("--output", required=True, type=Path, help="输出 PDF 路径（必填）")
    parser.add_argument("--pages", required=True, type=_positive_int, help="页数（正整数）")
    parser.add_argument(
        "--document-id",
        default=None,
        help="文档标识，默认从输出文件名派生",
    )
    parser.add_argument(
        "--text-length",
        type=int,
        default=DEFAULT_TEXT_LENGTH,
        help=f"每个正常页的目标文本长度（默认 {DEFAULT_TEXT_LENGTH}）",
    )
    parser.add_argument(
        "--no-unique-text",
        action="store_true",
        help="关闭每页唯一检索词（TOKEN 段）",
    )
    parser.add_argument(
        "--special-pages",
        default="",
        help='特殊页规格，如 "blank:7,short:8,rot90:9,wide:10,image:15"',
    )
    parser.add_argument(
        "--special-preset",
        action="store_true",
        help="使用固定混合预设（每种特殊页各一页，均匀分布）",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖已存在的输出文件（默认拒绝）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: parse arguments, build the PDF, print a summary."""

    args = build_parser().parse_args(argv)
    try:
        explicit = parse_special_pages(args.special_pages) if args.special_pages else {}
        specials = explicit
        if args.special_preset:
            specials = {**preset_special_pages(args.pages), **explicit}
        result = build_scale_pdf(
            args.output,
            pages=args.pages,
            document_id=args.document_id,
            text_length=args.text_length,
            unique_text=not args.no_unique_text,
            special_pages=specials,
            overwrite=args.overwrite,
        )
    except (ValueError, FileExistsError, FormalPathError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    # ASCII-only labels: the Windows console code page is not necessarily
    # UTF-8, and callers (tests, schedulers) may capture this output.
    print(f"Output: {result.path}")
    print(f"Pages: {result.pages}")
    print(f"Size: {result.size_bytes} bytes")
    print(f"Duration: {result.duration_seconds:.3f} s")
    print(f"SHA-256: {result.sha256}")
    return 0


def _add_page(
    document: pymupdf.Document,
    document_id: str,
    page_number: int,
    *,
    text_length: int,
    unique_text: bool,
    special: str | None,
) -> None:
    """Append one page, applying the requested special-page rule."""

    if special == "wide":
        page = document.new_page(width=WIDE_PAGE_WIDTH, height=WIDE_PAGE_HEIGHT)
    else:
        page = document.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)

    if special == "blank":
        return
    if special == "short":
        page.insert_text((72, 72), SHORT_PAGE_TEXT, fontsize=12)
        return
    if special == "image":
        _insert_pattern_image(page, page_number)
        return

    text = normal_page_text(
        document_id, page_number, text_length=text_length, unique_text=unique_text
    )
    page.insert_text((72, 72), text, fontsize=11)
    if special in _ROTATION_BY_TYPE:
        page.set_rotation(_ROTATION_BY_TYPE[special])


def _insert_pattern_image(page: pymupdf.Page, page_number: int) -> None:
    """Paint a deterministic striped pixmap into the page (no text layer)."""

    width, height = 240, 120
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    pixmap.clear_with(210)
    for y in range(height):
        for x in range(width):
            if (x + y + page_number) % 17 == 0:
                pixmap.set_pixel(x, y, (20, 60, 140))
            elif (x * 3 + page_number) % 29 == 0:
                pixmap.set_pixel(x, y, (140, 30, 30))
    page.insert_image(pymupdf.Rect(72, 72, 72 + width, 72 + height), pixmap=pixmap)


def _filler_text(page_number: int, target_length: int) -> str:
    """Return deterministic filler words with at least ``target_length`` chars."""

    if target_length <= 0:
        return ""
    words: list[str] = []
    total = 0
    index = page_number % len(_FILLER_WORDS)
    while total < target_length:
        word = _FILLER_WORDS[index % len(_FILLER_WORDS)]
        words.append(word)
        total += len(word) + 1
        index += 1
    return " ".join(words)


def _reject_formal_path(path: Path) -> None:
    """Refuse any output path inside the formal data directory."""

    resolved = path.resolve(strict=False)
    formal = FORMAL_PROJECT_ROOT.resolve(strict=False)
    if resolved == formal or formal in resolved.parents:
        raise FormalPathError(f"拒绝写入正式数据目录：{resolved}")


def _sha256_of_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"页数必须是正整数：{value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"页数必须是正整数：{value!r}")
    return number


if __name__ == "__main__":
    raise SystemExit(main())
