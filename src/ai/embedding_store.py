"""Page-embedding vector serialization contract (v0.5.0 Phase 7 prototype).

This module defines the single, dependency-free storage format for persisted
page embedding vectors: one format-version byte followed by ``dimensions``
little-endian IEEE-754 float32 values.

Why a versioned binary BLOB instead of JSON TEXT:

- deterministic decoding: fixed width, explicit byte order, no platform
  native-endian or float formatting ambiguity;
- compact: exactly ``1 + 4 * dimensions`` bytes, so the database itself can
  enforce length with a CHECK constraint;
- corruption detection: any length mismatch or unknown format version is
  detectable before a single float is read;
- forward migration: the version byte leaves room for a future format
  (for example float16 or quantized vectors) without a schema rewrite.

Hard boundaries:

- Pure Python, no I/O: no network, no database, no filesystem, no numpy.
- This module only (de)serializes vectors. It does not decide which page
  text is embedded, does not call any embedding API, and does not know
  about freshness; the persistence layer owns those concerns.

Failure semantics: invalid input is rejected with ``ValueError``
(fail-closed). Nothing is silently truncated, padded, or rounded into
place; NaN and Inf are refused in both directions.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from typing import Final

__all__ = [
    "EMBEDDING_VECTOR_FORMAT_VERSION",
    "decode_vector",
    "encode_vector",
    "vector_blob_size",
]

#: Storage format version byte. Bump only on a breaking format change.
EMBEDDING_VECTOR_FORMAT_VERSION: Final = 1

_BYTES_PER_FLOAT32: Final = 4
#: Largest finite magnitude representable in IEEE-754 binary32.
_FLOAT32_MAX: Final = 3.4028234663852886e38


def vector_blob_size(dimensions: int) -> int:
    """Return the exact byte length of a stored vector blob."""

    _validate_dimensions(dimensions)
    return 1 + _BYTES_PER_FLOAT32 * dimensions


def encode_vector(vector: Sequence[float], *, dimensions: int) -> bytes:
    """Serialize ``vector`` into the versioned float32 little-endian format.

    ``len(vector)`` must equal ``dimensions`` exactly; every component must
    be a finite real number within float32 range. Integers are accepted and
    converted to float; booleans are not numbers for this contract.
    """

    _validate_dimensions(dimensions)
    if isinstance(vector, (str, bytes, bytearray, memoryview)):
        raise ValueError(f"向量必须是数值序列：{type(vector).__name__}")
    values = tuple(vector)
    if not values:
        raise ValueError("向量不能为空")
    if len(values) != dimensions:
        raise ValueError(f"向量长度 {len(values)} 与 dimensions={dimensions} 不一致")
    floats: list[float] = []
    for component in values:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise ValueError(f"向量分量必须是有限数值：{component!r}")
        value = float(component)
        if not math.isfinite(value):
            raise ValueError(f"向量分量必须是有限数值：{component!r}")
        if abs(value) > _FLOAT32_MAX:
            raise ValueError(f"向量分量超出 float32 范围：{component!r}")
        floats.append(value)
    header = bytes([EMBEDDING_VECTOR_FORMAT_VERSION])
    return header + struct.pack(f"<{dimensions}f", *floats)


def decode_vector(blob: bytes, *, dimensions: int) -> tuple[float, ...]:
    """Parse a stored blob back into a vector, rejecting any corruption.

    The blob must carry the current format version and be exactly
    ``vector_blob_size(dimensions)`` bytes long; decoded values must all be
    finite. A malformed blob never yields a partially usable vector.
    """

    _validate_dimensions(dimensions)
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError(f"向量存储必须是字节串：{type(blob).__name__}")
    data = bytes(blob)
    expected = vector_blob_size(dimensions)
    if len(data) != expected:
        raise ValueError(
            f"向量字节长度 {len(data)} 与期望 {expected}（dimensions={dimensions}）不一致"
        )
    if data[0] != EMBEDDING_VECTOR_FORMAT_VERSION:
        raise ValueError(f"不支持的向量存储格式版本：{data[0]}")
    values = struct.unpack(f"<{dimensions}f", data[1:])
    for value in values:
        if not math.isfinite(value):
            raise ValueError(f"向量分量必须是有限数值：{value!r}")
    return tuple(float(value) for value in values)


def _validate_dimensions(dimensions: int) -> None:
    if isinstance(dimensions, bool) or not isinstance(dimensions, int):
        raise ValueError(f"dimensions 必须为正整数：{dimensions!r}")
    if dimensions <= 0:
        raise ValueError(f"dimensions 必须为正整数：{dimensions}")
