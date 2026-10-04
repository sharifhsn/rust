#!/usr/bin/env python3
"""Measure lossless codec profiles over a hash-pinned Rust artifact corpus.

The sweep works in memory one source artifact at a time. It writes records and logs under a new
output directory, never changes the corpus, and models the current Rust container's fixed
64-byte header and 24-byte per-chunk index entries. It is a codec/profile experiment, not a
benchmark of rustc's file I/O or its complete pack/unpack path.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import hashlib
import importlib.util
import json
import lzma
import os
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


HEADER_BYTES = 64
INDEX_ENTRY_BYTES = 24
SMALLEST_PACKABLE_BYTES = HEADER_BYTES + INDEX_ENTRY_BYTES
RANGE_PROBE_BYTES = 4096
SCRIPT_VERSION = 1


@dataclass(frozen=True)
class CodecSpec:
    name: str
    parameter: str
    profile: str

    @property
    def key(self) -> str:
        return f"{self.name}:{self.parameter}"


def specs_for(profile: str) -> list[CodecSpec]:
    sets: dict[str, list[tuple[str, str, str]]] = {
        "legacy": [("zstd", "3", "balanced")],
        "fast": [
            ("zstd", "-1", "fast"),
            ("lz4", "fast:1", "fast"),
            ("snappy", "default", "fast"),
            ("lzfse", "buffer", "fast"),
            ("libdeflate", "1", "fast"),
        ],
        "balanced": [
            ("zstd", "3", "balanced"),
            ("lz4", "hc:3", "balanced"),
            ("libdeflate", "6", "balanced"),
            ("zlib", "6", "balanced"),
            ("brotli", "4", "balanced"),
            ("lzfse", "buffer", "balanced"),
        ],
        "small": [
            ("zstd", "9", "small"),
            ("zstd", "19", "small-upper-bound"),
            ("lz4", "hc:9", "small"),
            ("libdeflate", "9", "small"),
            ("libdeflate", "12", "small-upper-bound"),
            ("zlib", "9", "small"),
            ("brotli", "7", "small"),
            ("lzma", "0", "slow-size-control"),
            ("lzma", "3", "slow-size-control"),
        ],
        "screen": [
            ("zstd", "-1", "fast"),
            ("zstd", "1", "fast"),
            ("zstd", "3", "balanced"),
            ("zstd", "9", "small"),
            ("zstd", "19", "small-upper-bound"),
            ("lz4", "fast:1", "fast"),
            ("lz4", "fast:4", "fast"),
            ("lz4", "hc:3", "balanced"),
            ("lz4", "hc:9", "small"),
            ("libdeflate", "1", "fast"),
            ("libdeflate", "6", "balanced"),
            ("libdeflate", "9", "small"),
            ("zlib", "1", "fast"),
            ("zlib", "6", "balanced"),
            ("zlib", "9", "small"),
            ("brotli", "1", "fast"),
            ("brotli", "4", "balanced"),
            ("snappy", "default", "fast"),
            ("lzfse", "buffer", "balanced"),
        ],
        "incremental": [
            ("zstd", "-1", "fast"),
            ("zstd", "3", "balanced"),
            ("zstd", "9", "small"),
            ("lz4", "fast:1", "fast"),
            ("lz4", "hc:3", "balanced"),
            ("libdeflate", "1", "fast"),
            ("libdeflate", "6", "balanced"),
            ("brotli", "4", "balanced"),
            ("snappy", "default", "fast"),
            ("lzfse", "buffer", "balanced"),
        ],
    }
    if profile not in sets:
        raise ValueError(f"unknown profile {profile!r}")
    return [CodecSpec(name, parameter, label) for name, parameter, label in sets[profile]]


def available_methods() -> dict[str, str]:
    result: dict[str, str] = {}
    for method in ("zstd", "lz4", "deflate", "snappy", "compression"):
        result[method] = ctypes.util.find_library(method) or "unavailable"
    result["brotli-python"] = "available" if importlib.util.find_spec("brotli") else "unavailable"
    result["python-zlib"] = zlib.ZLIB_RUNTIME_VERSION
    result["python-lzma"] = getattr(lzma, "LZMA_VERSION", "available")
    return result


def artifact_class(item: dict[str, Any]) -> str:
    path = item["relative_path"]
    basename = Path(path).name
    if basename.startswith("dep-graph") or basename == "dep-graph.bin":
        return "dep-graph"
    if basename.startswith("query-cache") or basename == "query-cache.bin":
        return "query-cache"
    if "work-products" in path or basename.startswith("work-products"):
        return "work-products"
    extension = Path(path).suffix.lower()
    if extension in (".rlib", ".rmeta", ".o", ".a", ".bc"):
        return {".rlib": "rlib", ".rmeta": "rmeta", ".o": "object", ".a": "archive", ".bc": "bitcode"}[extension]
    if basename.endswith(".bin"):
        return "cache-bin"
    return item.get("kind", "other")


def read_manifest(manifest_path: Path, root_arg: Path | None) -> tuple[Path, list[dict[str, Any]], str]:
    manifest_bytes = manifest_path.read_bytes()
    manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    data = json.loads(manifest_bytes)
    if data.get("schema_version") != 1 or not isinstance(data.get("artifacts"), list):
        raise ValueError(f"unsupported manifest schema in {manifest_path}")
    root = (root_arg or manifest_path.parent).resolve()
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data["artifacts"]:
        rel = raw.get("relative_path")
        if not isinstance(rel, str) or not rel or rel in seen:
            raise ValueError(f"invalid or duplicate relative_path in manifest: {rel!r}")
        seen.add(rel)
        path = (root / rel).resolve()
        if root not in path.parents:
            raise ValueError(f"artifact escapes corpus root: {rel}")
        expected_size = int(raw["size_bytes"])
        expected_hash = str(raw["sha256"]).lower()
        if len(expected_hash) != 64:
            raise ValueError(f"invalid sha256 for {rel}")
        stat = path.stat()
        if stat.st_size != expected_size:
            raise ValueError(f"size differs from manifest for {rel}: {stat.st_size} != {expected_size}")
        items.append({
            "relative_path": rel,
            "path": path,
            "kind": raw.get("kind", "other"),
            "class": artifact_class(raw),
            "size_bytes": expected_size,
            "sha256": expected_hash,
        })
    return root, sorted(items, key=lambda x: x["relative_path"]), manifest_hash


def source_digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def rust_container_size(raw_bytes: int, chunk_count: int, stored_payload_bytes: int) -> dict[str, int | bool]:
    if raw_bytes <= SMALLEST_PACKABLE_BYTES:
        return {
            "header_and_index_bytes": 0,
            "container_bytes": raw_bytes,
            "container_payload_bytes": raw_bytes,
            "kept_raw": True,
        }
    metadata_bytes = HEADER_BYTES + INDEX_ENTRY_BYTES * chunk_count
    candidate = metadata_bytes + stored_payload_bytes
    if candidate >= raw_bytes:
        return {
            "header_and_index_bytes": 0,
            "container_bytes": raw_bytes,
            "container_payload_bytes": raw_bytes,
            "kept_raw": True,
        }
    return {
        "header_and_index_bytes": metadata_bytes,
        "container_bytes": candidate,
        "container_payload_bytes": stored_payload_bytes,
        "kept_raw": False,
    }


class CodecBase:
    name = "base"
    parameter = ""

    def begin_artifact(self, max_chunk_size: int) -> Any:
        return None

    def compress(self, data: bytes, session: Any) -> bytes:
        raise NotImplementedError

    def decompress(self, encoded: bytes, raw_size: int, session: Any) -> bytes:
        raise NotImplementedError

    def end_artifact(self, session: Any) -> None:
        pass

    def context_memory(self, session: Any) -> int | None:
        return None

    def identity(self) -> dict[str, Any]:
        return {"method": self.name, "parameter": self.parameter}


class ZstdCodec(CodecBase):
    name = "zstd"

    def __init__(self, level: int):
        self.parameter = str(level)
        libname = ctypes.util.find_library("zstd")
        if not libname:
            raise RuntimeError("libzstd unavailable")
        self.lib = ctypes.CDLL(libname)
        self.lib.ZSTD_versionString.restype = ctypes.c_char_p
        self.lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
        self.lib.ZSTD_compressBound.restype = ctypes.c_size_t
        self.lib.ZSTD_createCCtx.restype = ctypes.c_void_p
        self.lib.ZSTD_freeCCtx.argtypes = [ctypes.c_void_p]
        self.lib.ZSTD_freeCCtx.restype = ctypes.c_size_t
        self.lib.ZSTD_createDCtx.restype = ctypes.c_void_p
        self.lib.ZSTD_freeDCtx.argtypes = [ctypes.c_void_p]
        self.lib.ZSTD_freeDCtx.restype = ctypes.c_size_t
        self.lib.ZSTD_compressCCtx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
        self.lib.ZSTD_compressCCtx.restype = ctypes.c_size_t
        self.lib.ZSTD_decompressDCtx.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        self.lib.ZSTD_decompressDCtx.restype = ctypes.c_size_t
        self.lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
        self.lib.ZSTD_isError.restype = ctypes.c_uint
        self.lib.ZSTD_getErrorName.argtypes = [ctypes.c_size_t]
        self.lib.ZSTD_getErrorName.restype = ctypes.c_char_p
        self.lib.ZSTD_sizeof_CCtx.argtypes = [ctypes.c_void_p]
        self.lib.ZSTD_sizeof_CCtx.restype = ctypes.c_size_t
        self.lib.ZSTD_sizeof_DCtx.argtypes = [ctypes.c_void_p]
        self.lib.ZSTD_sizeof_DCtx.restype = ctypes.c_size_t

    def begin_artifact(self, max_chunk_size: int) -> tuple[int, int]:
        cctx, dctx = self.lib.ZSTD_createCCtx(), self.lib.ZSTD_createDCtx()
        if not cctx or not dctx:
            raise MemoryError("could not allocate Zstandard contexts")
        return (cctx, dctx)

    def compress(self, data: bytes, session: tuple[int, int]) -> bytes:
        bound = int(self.lib.ZSTD_compressBound(len(data)))
        dst = ctypes.create_string_buffer(bound)
        result = int(self.lib.ZSTD_compressCCtx(session[0], dst, bound, ctypes.c_char_p(data), len(data), int(self.parameter)))
        if self.lib.ZSTD_isError(result):
            raise RuntimeError(self.lib.ZSTD_getErrorName(result).decode("utf-8", "replace"))
        return dst.raw[:result]

    def decompress(self, encoded: bytes, raw_size: int, session: tuple[int, int]) -> bytes:
        dst = ctypes.create_string_buffer(raw_size)
        result = int(self.lib.ZSTD_decompressDCtx(session[1], dst, raw_size, ctypes.c_char_p(encoded), len(encoded)))
        if self.lib.ZSTD_isError(result):
            raise RuntimeError(self.lib.ZSTD_getErrorName(result).decode("utf-8", "replace"))
        if result != raw_size:
            raise RuntimeError(f"zstd decoded {result} bytes; expected {raw_size}")
        return dst.raw[:result]

    def end_artifact(self, session: tuple[int, int]) -> None:
        self.lib.ZSTD_freeCCtx(session[0])
        self.lib.ZSTD_freeDCtx(session[1])

    def context_memory(self, session: tuple[int, int]) -> int:
        return int(self.lib.ZSTD_sizeof_CCtx(session[0]) + self.lib.ZSTD_sizeof_DCtx(session[1]))

    def identity(self) -> dict[str, Any]:
        return {**super().identity(), "library": ctypes.util.find_library("zstd"), "version": self.lib.ZSTD_versionString().decode()}


class Lz4Codec(CodecBase):
    name = "lz4"

    def __init__(self, mode: str, level: int):
        self.mode, self.level = mode, level
        self.parameter = f"{mode}:{level}"
        libname = ctypes.util.find_library("lz4")
        if not libname:
            raise RuntimeError("liblz4 unavailable")
        self.lib = ctypes.CDLL(libname)
        self.lib.LZ4_versionString.restype = ctypes.c_char_p
        self.lib.LZ4_compressBound.argtypes = [ctypes.c_int]
        self.lib.LZ4_compressBound.restype = ctypes.c_int
        self.lib.LZ4_decompress_safe.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        self.lib.LZ4_decompress_safe.restype = ctypes.c_int
        self.lib.LZ4_compress_fast.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.lib.LZ4_compress_fast.restype = ctypes.c_int
        self.lib.LZ4_compress_HC.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        self.lib.LZ4_compress_HC.restype = ctypes.c_int

    def compress(self, data: bytes, session: Any) -> bytes:
        bound = int(self.lib.LZ4_compressBound(len(data)))
        if bound <= 0:
            raise ValueError(f"LZ4 input too large: {len(data)}")
        dst = ctypes.create_string_buffer(bound)
        if self.mode == "fast":
            size = int(self.lib.LZ4_compress_fast(ctypes.c_char_p(data), dst, len(data), bound, self.level))
        else:
            size = int(self.lib.LZ4_compress_HC(ctypes.c_char_p(data), dst, len(data), bound, self.level))
        if size <= 0:
            raise RuntimeError("LZ4 compression failed")
        return dst.raw[:size]

    def decompress(self, encoded: bytes, raw_size: int, session: Any) -> bytes:
        dst = ctypes.create_string_buffer(raw_size)
        size = int(self.lib.LZ4_decompress_safe(ctypes.c_char_p(encoded), dst, len(encoded), raw_size))
        if size != raw_size:
            raise RuntimeError(f"LZ4 decoded {size} bytes; expected {raw_size}")
        return dst.raw[:size]

    def identity(self) -> dict[str, Any]:
        return {**super().identity(), "library": ctypes.util.find_library("lz4"), "version": self.lib.LZ4_versionString().decode()}


class SnappyCodec(CodecBase):
    name = "snappy"
    parameter = "default"

    def __init__(self):
        libname = ctypes.util.find_library("snappy")
        if not libname:
            raise RuntimeError("libsnappy unavailable")
        self.lib = ctypes.CDLL(libname)
        self.lib.snappy_max_compressed_length.argtypes = [ctypes.c_size_t]
        self.lib.snappy_max_compressed_length.restype = ctypes.c_size_t
        self.lib.snappy_compress.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        self.lib.snappy_compress.restype = ctypes.c_int
        self.lib.snappy_uncompress.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t)]
        self.lib.snappy_uncompress.restype = ctypes.c_int

    def compress(self, data: bytes, session: Any) -> bytes:
        capacity = int(self.lib.snappy_max_compressed_length(len(data)))
        dst = ctypes.create_string_buffer(capacity)
        out_len = ctypes.c_size_t(capacity)
        status = self.lib.snappy_compress(ctypes.c_char_p(data), len(data), dst, ctypes.byref(out_len))
        if status != 0:
            raise RuntimeError(f"snappy compression status {status}")
        return dst.raw[:out_len.value]

    def decompress(self, encoded: bytes, raw_size: int, session: Any) -> bytes:
        dst = ctypes.create_string_buffer(raw_size)
        out_len = ctypes.c_size_t(raw_size)
        status = self.lib.snappy_uncompress(ctypes.c_char_p(encoded), len(encoded), dst, ctypes.byref(out_len))
        if status != 0 or out_len.value != raw_size:
            raise RuntimeError(f"snappy decompression status {status}, size {out_len.value}, expected {raw_size}")
        return dst.raw[:out_len.value]

    def identity(self) -> dict[str, Any]:
        return {**super().identity(), "library": ctypes.util.find_library("snappy"), "version": "runtime C API does not expose a version string"}


class LibdeflateCodec(CodecBase):
    name = "libdeflate"

    def __init__(self, level: int):
        self.parameter = str(level)
        libname = ctypes.util.find_library("deflate")
        if not libname:
            raise RuntimeError("libdeflate unavailable")
        self.lib = ctypes.CDLL(libname)
        self.lib.libdeflate_alloc_compressor.argtypes = [ctypes.c_int]
        self.lib.libdeflate_alloc_compressor.restype = ctypes.c_void_p
        self.lib.libdeflate_free_compressor.argtypes = [ctypes.c_void_p]
        self.lib.libdeflate_alloc_decompressor.restype = ctypes.c_void_p
        self.lib.libdeflate_free_decompressor.argtypes = [ctypes.c_void_p]
        self.lib.libdeflate_deflate_compress_bound.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        self.lib.libdeflate_deflate_compress_bound.restype = ctypes.c_size_t
        self.lib.libdeflate_deflate_compress.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
        self.lib.libdeflate_deflate_compress.restype = ctypes.c_size_t
        self.lib.libdeflate_deflate_decompress.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        self.lib.libdeflate_deflate_decompress.restype = ctypes.c_int

    def begin_artifact(self, max_chunk_size: int) -> tuple[int, int]:
        comp = self.lib.libdeflate_alloc_compressor(int(self.parameter))
        decomp = self.lib.libdeflate_alloc_decompressor()
        if not comp or not decomp:
            raise MemoryError("libdeflate context allocation failed")
        return comp, decomp

    def compress(self, data: bytes, session: tuple[int, int]) -> bytes:
        bound = int(self.lib.libdeflate_deflate_compress_bound(session[0], len(data)))
        dst = ctypes.create_string_buffer(bound)
        size = int(self.lib.libdeflate_deflate_compress(session[0], ctypes.c_char_p(data), len(data), dst, bound))
        if size <= 0:
            raise RuntimeError("libdeflate compression failed")
        return dst.raw[:size]

    def decompress(self, encoded: bytes, raw_size: int, session: tuple[int, int]) -> bytes:
        dst = ctypes.create_string_buffer(raw_size)
        actual = ctypes.c_size_t()
        status = self.lib.libdeflate_deflate_decompress(session[1], ctypes.c_char_p(encoded), len(encoded), dst, raw_size, ctypes.byref(actual))
        if status != 0 or actual.value != raw_size:
            raise RuntimeError(f"libdeflate status {status}, size {actual.value}, expected {raw_size}")
        return dst.raw[:actual.value]

    def end_artifact(self, session: tuple[int, int]) -> None:
        self.lib.libdeflate_free_compressor(session[0])
        self.lib.libdeflate_free_decompressor(session[1])

    def identity(self) -> dict[str, Any]:
        header_version = None
        for path in (Path("/opt/homebrew/include/libdeflate.h"), Path("/usr/include/libdeflate.h")):
            if path.is_file():
                for line in path.read_text(errors="replace").splitlines():
                    if "LIBDEFLATE_VERSION_STRING" in line and '"' in line:
                        header_version = line.split('"')[1]
                        break
        return {**super().identity(), "library": ctypes.util.find_library("deflate"), "header_version": header_version, "version_note": "runtime C API has no version getter"}


class ZlibCodec(CodecBase):
    name = "zlib"

    def __init__(self, level: int):
        self.parameter = str(level)
        self.level = level

    def compress(self, data: bytes, session: Any) -> bytes:
        compressor = zlib.compressobj(self.level, zlib.DEFLATED, -15)
        return compressor.compress(data) + compressor.flush()

    def decompress(self, encoded: bytes, raw_size: int, session: Any) -> bytes:
        return zlib.decompress(encoded, -15)

    def identity(self) -> dict[str, Any]:
        return {**super().identity(), "version": zlib.ZLIB_RUNTIME_VERSION, "compile_version": zlib.ZLIB_VERSION}


class BrotliCodec(CodecBase):
    name = "brotli"

    def __init__(self, quality: int):
        try:
            import brotli
        except ImportError as error:
            raise RuntimeError("Python brotli module unavailable") from error
        self.brotli = brotli
        self.quality = quality
        self.parameter = str(quality)

    def compress(self, data: bytes, session: Any) -> bytes:
        return self.brotli.compress(data, quality=self.quality, lgwin=22)

    def decompress(self, encoded: bytes, raw_size: int, session: Any) -> bytes:
        return self.brotli.decompress(encoded)

    def identity(self) -> dict[str, Any]:
        return {**super().identity(), "version": getattr(self.brotli, "__version__", "unknown"), "module": getattr(self.brotli, "__file__", None), "lgwin": 22}


class LzmaCodec(CodecBase):
    name = "lzma"

    def __init__(self, preset: int):
        self.parameter = str(preset)
        self.preset = preset

    def compress(self, data: bytes, session: Any) -> bytes:
        filters = [{"id": lzma.FILTER_LZMA2, "preset": self.preset}]
        return lzma.compress(data, format=lzma.FORMAT_RAW, filters=filters)

    def decompress(self, encoded: bytes, raw_size: int, session: Any) -> bytes:
        filters = [{"id": lzma.FILTER_LZMA2, "preset": self.preset}]
        return lzma.decompress(encoded, format=lzma.FORMAT_RAW, filters=filters)

    def identity(self) -> dict[str, Any]:
        return {**super().identity(), "python_module": getattr(lzma, "__file__", None), "xz_cli": command_version(["xz", "--version"])}


class AppleCompressionCodec(CodecBase):
    name = "lzfse"
    parameter = "buffer"
    ALGORITHM_LZFSE = 0x801

    def __init__(self):
        libname = ctypes.util.find_library("compression")
        if not libname:
            raise RuntimeError("Apple Compression framework unavailable")
        self.lib = ctypes.CDLL(libname)
        self.lib.compression_encode_buffer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        self.lib.compression_encode_buffer.restype = ctypes.c_size_t
        self.lib.compression_decode_buffer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_int]
        self.lib.compression_decode_buffer.restype = ctypes.c_size_t
        self.lib.compression_encode_scratch_buffer_size.argtypes = [ctypes.c_int]
        self.lib.compression_encode_scratch_buffer_size.restype = ctypes.c_size_t
        self.lib.compression_decode_scratch_buffer_size.argtypes = [ctypes.c_int]
        self.lib.compression_decode_scratch_buffer_size.restype = ctypes.c_size_t

    def begin_artifact(self, max_chunk_size: int) -> tuple[Any, Any, Any, Any]:
        algo = self.ALGORITHM_LZFSE
        enc_size = int(self.lib.compression_encode_scratch_buffer_size(algo))
        dec_size = int(self.lib.compression_decode_scratch_buffer_size(algo))
        enc = ctypes.create_string_buffer(enc_size) if enc_size else None
        dec = ctypes.create_string_buffer(dec_size) if dec_size else None
        # Apple's buffer API has no bound helper. Retry with a larger destination if needed.
        return enc, dec, enc_size, dec_size

    def compress(self, data: bytes, session: tuple[Any, Any, Any, Any]) -> bytes:
        capacity = max(4096, len(data) + len(data) // 4 + 65536)
        for _ in range(4):
            dst = ctypes.create_string_buffer(capacity)
            size = int(self.lib.compression_encode_buffer(dst, capacity, ctypes.c_char_p(data), len(data), session[0], self.ALGORITHM_LZFSE))
            if size:
                return dst.raw[:size]
            capacity *= 2
        raise RuntimeError("Apple Compression LZFSE did not fit the expanding output buffers")

    def decompress(self, encoded: bytes, raw_size: int, session: tuple[Any, Any, Any, Any]) -> bytes:
        dst = ctypes.create_string_buffer(raw_size)
        size = int(self.lib.compression_decode_buffer(dst, raw_size, ctypes.c_char_p(encoded), len(encoded), session[1], self.ALGORITHM_LZFSE))
        if size != raw_size:
            raise RuntimeError(f"Apple LZFSE decoded {size} bytes; expected {raw_size}")
        return dst.raw[:size]

    def context_memory(self, session: tuple[Any, Any, Any, Any]) -> int:
        return int(session[2] + session[3])

    def identity(self) -> dict[str, Any]:
        return {**super().identity(), "library": ctypes.util.find_library("compression"), "framework_algorithm": "COMPRESSION_LZFSE (0x801)", "context_memory_note": "encode/decode scratch sizes reported by Compression API"}


def command_version(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0 or result.stdout or result.stderr:
        return (result.stdout + result.stderr).strip().splitlines()[0:1][0] if (result.stdout + result.stderr).strip() else None
    return None


def command_output(argv: list[str]) -> str | None:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (result.stdout + result.stderr).strip()
    return output if output else None


def build_codec(spec: CodecSpec) -> CodecBase:
    if spec.name == "zstd":
        return ZstdCodec(int(spec.parameter))
    if spec.name == "lz4":
        mode, level = spec.parameter.split(":", 1)
        return Lz4Codec(mode, int(level))
    if spec.name == "snappy":
        return SnappyCodec()
    if spec.name == "libdeflate":
        return LibdeflateCodec(int(spec.parameter))
    if spec.name == "zlib":
        return ZlibCodec(int(spec.parameter))
    if spec.name == "brotli":
        return BrotliCodec(int(spec.parameter))
    if spec.name == "lzma":
        return LzmaCodec(int(spec.parameter))
    if spec.name == "lzfse":
        return AppleCompressionCodec()
    raise ValueError(f"unknown codec {spec.name}")


def chunk_ranges(length: int, chunk_size: int) -> list[tuple[int, int]]:
    return [(start, min(length, start + chunk_size)) for start in range(0, length, chunk_size)]


def resolved_chunk_size(choice: str, file_size: int) -> int:
    if choice == "whole":
        return max(1, file_size)
    if choice.endswith("KiB"):
        return int(choice[:-3]) * 1024
    if choice.endswith("MiB"):
        return int(choice[:-3]) * 1024 * 1024
    raise ValueError(f"invalid chunk size {choice!r}")


def range_offsets(file_size: int, length: int = RANGE_PROBE_BYTES) -> list[int]:
    if not file_size:
        return []
    size = min(length, file_size)
    candidates = [0, max(0, file_size // 2 - size // 2), max(0, file_size - size)]
    return list(dict.fromkeys(candidates))


def current_peak_rss_bytes() -> int | None:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    if value <= 0:
        return None
    # macOS reports bytes; Linux and most other Unix systems report KiB.
    return value if sys.platform == "darwin" else value * 1024


def run_worker(manifest_path: Path, root_arg: Path | None, selection_path: Path | None, config: dict[str, Any], destination: Path) -> dict[str, Any]:
    root, items, manifest_hash = read_manifest(manifest_path, root_arg)
    if selection_path is not None:
        selected = set(json.loads(selection_path.read_text()))
        items = [item for item in items if item["relative_path"] in selected]
        if len(items) != len(selected):
            raise ValueError(f"worker selection has {len(selected)} paths but manifest resolved {len(items)}")
    spec = CodecSpec(config["method"], config["parameter"], config["profile"])
    codec = build_codec(spec)
    chunk_choice = config["chunk_size"]
    by_class: dict[str, dict[str, int]] = {}
    artifact_records: list[dict[str, Any]] = []
    totals = {
        "raw_bytes": 0,
        "chunk_count": 0,
        "encoded_payload_bytes": 0,
        "effective_payload_bytes": 0,
        "container_bytes": 0,
        "header_and_index_bytes": 0,
        "container_payload_bytes": 0,
        "kept_raw_files": 0,
        "raw_chunks": 0,
        "compressed_chunks": 0,
        "range_probe_bytes_requested": 0,
        "range_probe_payload_bytes_read": 0,
        "range_probe_raw_bytes_decoded": 0,
        "range_probe_direct_bytes_read": 0,
    }
    pack_wall = pack_cpu = unpack_codec_wall = unpack_codec_cpu = 0.0
    unpack_format_wall = unpack_format_cpu = range_wall = range_cpu = 0.0
    context_memory_max = 0
    roundtrip_checked = 0
    codec_chunks_verified = 0
    run_started = datetime.now(timezone.utc).isoformat()

    for index, item in enumerate(items, start=1):
        data = item["path"].read_bytes()
        if len(data) != item["size_bytes"]:
            raise ValueError(f"corpus artifact changed size: {item['relative_path']}")
        expected_hash = item["sha256"]
        input_hash = source_digest(data)
        if input_hash != expected_hash:
            raise ValueError(f"corpus artifact hash differs from manifest: {item['relative_path']}")

        chunk_size = resolved_chunk_size(chunk_choice, len(data))
        ranges = chunk_ranges(len(data), chunk_size)
        session = codec.begin_artifact(chunk_size)
        session_memory = codec.context_memory(session)
        if session_memory is not None:
            context_memory_max = max(context_memory_max, session_memory)

        encode_started_wall = time.perf_counter()
        encode_started_cpu = time.process_time()
        encoded_chunks: list[tuple[bytes, int, bool]] = []
        encoded_payload_bytes = 0
        stored_payload_bytes = 0
        raw_chunk_count = 0
        for start, end in ranges:
            raw_chunk = data[start:end]
            encoded = codec.compress(raw_chunk, session)
            encoded_payload_bytes += len(encoded)
            if len(encoded) < len(raw_chunk):
                encoded_chunks.append((encoded, len(raw_chunk), False))
                stored_payload_bytes += len(encoded)
            else:
                encoded_chunks.append((raw_chunk, len(raw_chunk), True))
                stored_payload_bytes += len(raw_chunk)
                raw_chunk_count += 1
        session_memory_after_pack = codec.context_memory(session)
        if session_memory_after_pack is not None:
            context_memory_max = max(context_memory_max, session_memory_after_pack)
        pack_wall += time.perf_counter() - encode_started_wall
        pack_cpu += time.process_time() - encode_started_cpu

        format_size = rust_container_size(len(data), len(ranges), stored_payload_bytes)
        decode_started_wall = time.perf_counter()
        decode_started_cpu = time.process_time()
        hasher = hashlib.sha256()
        output_len = 0
        if format_size["kept_raw"]:
            # The modeled container would retain the original file, so its read path bypasses
            # all candidate compressed chunks. Hashing verifies those stored raw bytes.
            hasher.update(data)
            output_len = len(data)
        else:
            for encoded, raw_len, kept_raw in encoded_chunks:
                if kept_raw:
                    decoded = encoded
                else:
                    codec_started_wall = time.perf_counter()
                    codec_started_cpu = time.process_time()
                    decoded = codec.decompress(encoded, raw_len, session)
                    unpack_codec_wall += time.perf_counter() - codec_started_wall
                    unpack_codec_cpu += time.process_time() - codec_started_cpu
                    codec_chunks_verified += 1
                if len(decoded) != raw_len:
                    raise RuntimeError(f"{spec.key} decoded wrong length in {item['relative_path']}")
                hasher.update(decoded)
                output_len += len(decoded)
        unpack_format_wall += time.perf_counter() - decode_started_wall
        unpack_format_cpu += time.process_time() - decode_started_cpu
        if output_len != len(data) or hasher.hexdigest() != expected_hash:
            raise RuntimeError(f"round-trip SHA-256 mismatch for {spec.key} {item['relative_path']}")
        roundtrip_checked += 1

        # Simulate three 4 KiB source ranges. Chunks are independent by construction; this
        # measures decompression amplification, not file seek/index I/O. Eager loading remains
        # the compiler's measured behavior until callers use its range-read API.
        range_requests: list[tuple[int, int, range]] = []
        probes = range_offsets(len(data))
        for offset in probes:
            probe_end = min(len(data), offset + RANGE_PROBE_BYTES)
            if probe_end > offset:
                first = offset // chunk_size
                last = (probe_end - 1) // chunk_size
                totals["range_probe_bytes_requested"] += probe_end - offset
                range_requests.append((offset, probe_end, range(first, last + 1)))
        range_started_wall = time.perf_counter()
        range_started_cpu = time.process_time()
        range_decoded_chunks = 0
        for offset, probe_end, chunk_indexes in range_requests:
            if format_size["kept_raw"]:
                direct = data[offset:probe_end]
                if len(direct) != probe_end - offset:
                    raise RuntimeError(f"raw range-probe returned a short slice for {item['relative_path']} at {offset}")
                totals["range_probe_direct_bytes_read"] += probe_end - offset
                continue
            pieces: list[bytes] = []
            for chunk_index in chunk_indexes:
                encoded, raw_len, kept_raw = encoded_chunks[chunk_index]
                if kept_raw:
                    decoded = encoded
                else:
                    decoded = codec.decompress(encoded, raw_len, session)
                chunk_start, chunk_end = ranges[chunk_index]
                overlap_start = max(offset, chunk_start) - chunk_start
                overlap_end = min(probe_end, chunk_end) - chunk_start
                pieces.append(decoded[overlap_start:overlap_end])
                totals["range_probe_raw_bytes_decoded"] += raw_len
                totals["range_probe_payload_bytes_read"] += len(encoded)
                range_decoded_chunks += 1
            if b"".join(pieces) != data[offset:probe_end]:
                raise RuntimeError(f"range-probe mismatch for {spec.key} {item['relative_path']} at {offset}")
        range_wall += time.perf_counter() - range_started_wall
        range_cpu += time.process_time() - range_started_cpu
        session_memory_after_decode = codec.context_memory(session)
        if session_memory_after_decode is not None:
            context_memory_max = max(context_memory_max, session_memory_after_decode)
        codec.end_artifact(session)

        record = {
            "relative_path": item["relative_path"],
            "class": item["class"],
            "kind": item["kind"],
            "raw_bytes": len(data),
            "chunks": len(ranges),
            "chunk_size": chunk_size,
            "chunk_size_choice": chunk_choice,
            "input_sha256": input_hash,
            "decoded_sha256": hasher.hexdigest(),
            "encoded_payload_bytes": encoded_payload_bytes,
            "candidate_stored_payload_bytes": stored_payload_bytes,
            "effective_payload_bytes": format_size["container_payload_bytes"],
            **format_size,
            "raw_chunks": raw_chunk_count,
            "compressed_chunks": len(ranges) - raw_chunk_count,
            "range_probe_count": len(probes),
            "range_chunks_decoded": range_decoded_chunks,
            "range_direct_bytes_read": (sum(min(len(data), offset + RANGE_PROBE_BYTES) - offset for offset in probes) if format_size["kept_raw"] else 0),
            "codec_chunks_verified": sum(1 for _, _, kept_raw in encoded_chunks if not kept_raw and not format_size["kept_raw"]),
        }
        artifact_records.append(record)
        for key in ("raw_bytes", "chunk_count", "encoded_payload_bytes", "effective_payload_bytes", "container_bytes", "header_and_index_bytes", "container_payload_bytes", "raw_chunks", "compressed_chunks", "range_probe_bytes_requested", "range_probe_payload_bytes_read", "range_probe_raw_bytes_decoded"):
            source_key = {"raw_bytes": "raw_bytes", "chunk_count": "chunks"}.get(key, key)
            totals[key] += int(record[source_key]) if source_key in record else int(format_size.get(source_key, 0))
        if record["kept_raw"]:
            totals["kept_raw_files"] += 1
        bucket = by_class.setdefault(item["class"], {"files": 0, "raw_bytes": 0, "container_bytes": 0, "saved_bytes": 0})
        bucket["files"] += 1
        bucket["raw_bytes"] += len(data)
        bucket["container_bytes"] += int(format_size["container_bytes"])
        bucket["saved_bytes"] += len(data) - int(format_size["container_bytes"])

    result = {
        "schema_version": SCRIPT_VERSION,
        "started_utc": run_started,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "manifest_path": str(manifest_path.resolve()),
        "manifest_sha256": manifest_hash,
        "corpus_root": str(root),
        "profile": spec.profile,
        "codec": codec.identity(),
        "chunk_size_choice": chunk_choice,
        "container_model": {"header_bytes": HEADER_BYTES, "index_entry_bytes": INDEX_ENTRY_BYTES, "raw_chunk_fallback": True, "whole_file_final_fallback": True},
        "timing_scope": "in-memory codec and wrapper work; excludes source file reads, output file writes, input SHA-256, and output hashing from codec-only timers; separate format-read timer includes output hashing",
        "pack_codec_wall_seconds": pack_wall,
        "pack_codec_cpu_seconds": pack_cpu,
        "unpack_codec_wall_seconds": unpack_codec_wall,
        "unpack_codec_cpu_seconds": unpack_codec_cpu,
        "unpack_format_wall_seconds": unpack_format_wall,
        "unpack_format_cpu_seconds": unpack_format_cpu,
        "range_probe_wall_seconds": range_wall,
        "range_probe_cpu_seconds": range_cpu,
        "process_peak_rss_bytes": current_peak_rss_bytes(),
        "process_peak_rss_source": "getrusage(RUSAGE_SELF).ru_maxrss; includes Python runtime and one file's input/encoded buffers; OS high-water, not codec-only memory",
        "reported_persistent_context_bytes_max": context_memory_max or None,
        "reported_persistent_context_bytes_note": "codec API-reported context/scratch only when available; does not include transient allocations or library-owned static tables",
        "roundtrip_files_verified": roundtrip_checked,
        "compressed_codec_chunks_verified": codec_chunks_verified,
        "totals": totals,
        "by_class": by_class,
        "artifacts": artifact_records,
    }
    destination.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def preflight_hashes(items: list[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    classes: dict[str, dict[str, int]] = {}
    for item in items:
        path = item["path"]
        if path.stat().st_size != item["size_bytes"]:
            raise ValueError(f"corpus size changed: {item['relative_path']}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item["sha256"]:
            raise ValueError(f"corpus hash changed: {item['relative_path']}")
        total += item["size_bytes"]
        bucket = classes.setdefault(item["class"], {"files": 0, "bytes": 0})
        bucket["files"] += 1
        bucket["bytes"] += item["size_bytes"]
    return {"artifact_count": len(items), "logical_bytes": total, "by_class": classes}


def host_load() -> list[float] | None:
    try:
        return [float(value) for value in os.getloadavg()]
    except (AttributeError, OSError):
        return None


def environment_record() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "python_executable": sys.executable,
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
        "lzma_module": getattr(lzma, "__file__", None),
        "codec_libraries": available_methods(),
        "cli_versions": {name: command_version([name, "--version"]) for name in ("zstd", "lz4", "brotli", "xz", "clang", "ar")},
        "brew_versions": command_output(["brew", "list", "--versions", "zstd", "lz4", "libdeflate", "snappy", "brotli", "xz"]),
    }


def run_parent(args: argparse.Namespace) -> int:
    manifest_path = args.manifest.resolve()
    root, items, manifest_hash = read_manifest(manifest_path, args.corpus)
    if args.artifact_class:
        classes = set(args.artifact_class)
        items = [item for item in items if item["class"] in classes]
        if not items:
            raise ValueError(f"artifact class filter matched no files: {sorted(classes)}")
    if args.limit is not None:
        items = items[:args.limit]
    preflight = preflight_hashes(items)
    if args.codec_spec:
        specs = []
        for value in args.codec_spec:
            if "=" not in value:
                raise ValueError(f"codec spec must have METHOD=PARAMETER form: {value!r}")
            method, parameter = value.split("=", 1)
            if not method or not parameter:
                raise ValueError(f"codec spec must have non-empty method and parameter: {value!r}")
            specs.append(CodecSpec(method, parameter, "custom"))
    else:
        specs = [spec for profile in args.profiles for spec in specs_for(profile)]
    # Keep a single copy of any shared exact codec/level request while preserving first profile.
    unique_specs: dict[tuple[str, str], CodecSpec] = {}
    for spec in specs:
        unique_specs.setdefault((spec.name, spec.parameter), spec)
    profile_set = "custom" if args.codec_spec else ",".join(args.profiles)
    configs = [{"method": spec.name, "parameter": spec.parameter, "profile": spec.profile, "chunk_size": chunk, "profile_set": profile_set} for spec in unique_specs.values() for chunk in args.chunk_sizes]
    if args.max_configs is not None:
        configs = configs[:args.max_configs]
    if not configs:
        raise ValueError("no codec configurations selected")

    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"output directory must be new or empty: {out}")
    out.mkdir(parents=True, exist_ok=True)
    log_dir = out / "logs"
    worker_dir = out / "workers"
    log_dir.mkdir(exist_ok=True)
    worker_dir.mkdir(exist_ok=True)
    selection_path = out / "selection.json"
    selection_path.write_text(json.dumps([item["relative_path"] for item in items], indent=2) + "\n")
    started = datetime.now(timezone.utc).isoformat()
    run_info = {
        "schema_version": SCRIPT_VERSION,
        "started_utc": started,
        "status": "running",
        "command": sys.argv,
        "script_path": str(Path(__file__).resolve()),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_hash,
        "corpus_root": str(root),
        "corpus_preflight": preflight,
        "profiles": ["custom"] if args.codec_spec else args.profiles,
        "codec_specs": args.codec_spec or None,
        "chunk_sizes": args.chunk_sizes,
        "repetitions": args.repetitions,
        "seed": args.seed,
        "configuration_count_per_repetition": len(configs),
        "environment": environment_record(),
        "method_availability": available_methods(),
        "measurement_caveats": [
            "Codec/profile results use full retained artifacts and independent chunks, but time in-memory codec/wrapper work rather than filesystem reads/writes or Cargo/rustc.",
            "The runner is Python-based: ctypes/native APIs are called from CPython, while zlib, Brotli, and LZMA use Python wrappers. CPU and wall times screen candidates; they are not predictions of the Rust implementation's overhead or end-to-end build time.",
            "The modeled file size includes the prototype's 64-byte header and 24-byte index entry per chunk, raw-chunk fallback, and whole-file raw fallback; it excludes directory/APFS allocation and filesystem metadata.",
            "Unpack phase time includes per-file SHA-256 verification; input reads and preflight source hashing are outside codec timers.",
            "Range probes decode only independent chunks that intersect three representative 4 KiB slices. They exclude index parsing, storage reads, and slice extraction; current compiler metadata consumers remain eager.",
            "RSS is the isolated worker process high-water and includes Python, codec library, and one artifact's data. It is not a direct measure of native codec-only scratch memory.",
            "Results describe the pinned corpus, installed native codec builds, and this machine; they are not general codec rankings.",
        ],
    }
    (out / "run.json").write_text(json.dumps(run_info, indent=2, sort_keys=True) + "\n")
    (out / "commands.jsonl").write_text("")
    (out / "trials.jsonl").write_text("")
    order_rng = random.Random(args.seed)
    total_trials = len(configs) * args.repetitions
    completed = 0
    start_clock = time.monotonic()
    print(f"corpus: {len(items)} artifacts, {preflight['logical_bytes']} bytes; {len(configs)} configs x {args.repetitions} reps = {total_trials} worker trials", flush=True)
    for repetition in range(1, args.repetitions + 1):
        order = list(configs)
        order_rng.shuffle(order)
        for config_index, config in enumerate(order, start=1):
            completed += 1
            config = {**config, "repetition": repetition, "trial": completed}
            slug = f"{completed:04d}-rep{repetition}-{config['method']}-{config['parameter'].replace(':', '_')}-{config['chunk_size']}".replace("/", "_")
            worker_result = worker_dir / f"{slug}.json"
            worker_log = log_dir / f"{slug}.log"
            command = [sys.executable, str(Path(__file__).resolve()), "--_worker-manifest", str(manifest_path), "--_worker-root", str(root), "--_worker-selection", str(selection_path), "--_worker-config", json.dumps(config, separators=(",", ":")), "--_worker-result", str(worker_result)]
            with (out / "commands.jsonl").open("a") as cmdlog:
                cmdlog.write(json.dumps({"trial": completed, "repetition": repetition, "config": config, "argv": command}) + "\n")
            with worker_log.open("wb") as log:
                worker_started = time.perf_counter()
                load_before = host_load()
                try:
                    result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, check=False, timeout=args.timeout_seconds)
                    worker_elapsed = time.perf_counter() - worker_started
                except subprocess.TimeoutExpired:
                    worker_elapsed = time.perf_counter() - worker_started
                    result = None
                load_after = host_load()
            if result is None:
                trial = {"schema_version": SCRIPT_VERSION, "status": "skipped_timeout", "error": f"worker exceeded {args.timeout_seconds}s", "config": config, "worker_elapsed_seconds": worker_elapsed}
            elif result.returncode != 0 or not worker_result.exists():
                log_tail = worker_log.read_text(errors="replace")[-4000:]
                trial = {"schema_version": SCRIPT_VERSION, "status": "error", "error": f"worker exit {result.returncode}", "log_tail": log_tail, "config": config, "worker_elapsed_seconds": worker_elapsed}
            else:
                trial = json.loads(worker_result.read_text())
                trial["status"] = "complete"
                trial["worker_elapsed_seconds"] = worker_elapsed
            trial["trial"] = completed
            trial["repetition"] = repetition
            trial["config"] = config
            trial["worker_log"] = str(worker_log.relative_to(out))
            trial["host_load_before"] = load_before
            trial["host_load_after"] = load_after
            with (out / "trials.jsonl").open("a") as results:
                results.write(json.dumps(trial, sort_keys=True) + "\n")
            elapsed = time.monotonic() - start_clock
            eta = (elapsed / completed) * (total_trials - completed) if completed else 0
            if trial["status"] == "complete":
                t = trial["totals"]
                saved = t["raw_bytes"] - t["container_bytes"]
                details = f"saved={saved}B pack={trial['pack_codec_wall_seconds']:.3f}s unpack={trial['unpack_codec_wall_seconds']:.3f}s range={trial['range_probe_wall_seconds']:.3f}s rss={trial['process_peak_rss_bytes']}"
            else:
                details = f"{trial['status']} {trial.get('error')}"
            print(f"[{completed}/{total_trials}] rep{repetition} {config['method']}:{config['parameter']} {config['chunk_size']} {details} ETA~{eta:.0f}s", flush=True)
    post_root, post_items, post_manifest_hash = read_manifest(manifest_path, args.corpus)
    if post_manifest_hash != manifest_hash:
        raise ValueError("manifest changed during sweep")
    postflight = preflight_hashes([item for item in post_items if item["relative_path"] in {x["relative_path"] for x in items}])
    if postflight["artifact_count"] != preflight["artifact_count"] or postflight["logical_bytes"] != preflight["logical_bytes"]:
        raise ValueError("corpus changed during sweep")
    run_info["finished_utc"] = datetime.now(timezone.utc).isoformat()
    run_info["status"] = "complete"
    run_info["corpus_postflight"] = postflight
    run_info["elapsed_wall_seconds"] = time.monotonic() - start_clock
    run_info["worker_trials"] = total_trials
    run_info["summary"] = make_summary(out / "trials.jsonl")
    (out / "run.json").write_text(json.dumps(run_info, indent=2, sort_keys=True) + "\n")
    (out / "summary.json").write_text(json.dumps(run_info["summary"], indent=2, sort_keys=True) + "\n")
    print(f"complete: {out}; {total_trials} trials; {run_info['elapsed_wall_seconds']:.1f}s", flush=True)
    return 0


def make_summary(trials_path: Path) -> dict[str, Any]:
    rows = [json.loads(line) for line in trials_path.read_text().splitlines() if line.strip()]
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = ":".join((row["config"]["method"], row["config"]["parameter"], row["config"]["chunk_size"]))
        groups.setdefault(key, []).append(row)
    output = []
    for key, members in sorted(groups.items()):
        complete = [member for member in members if member.get("status") == "complete"]
        record: dict[str, Any] = {"key": key, "trials": len(members), "completed": len(complete), "skipped_or_failed": len(members) - len(complete)}
        if complete:
            fields = ("pack_codec_wall_seconds", "pack_codec_cpu_seconds", "unpack_codec_wall_seconds", "unpack_codec_cpu_seconds", "unpack_format_wall_seconds", "unpack_format_cpu_seconds", "range_probe_wall_seconds", "range_probe_cpu_seconds", "process_peak_rss_bytes")
            record["median"] = {field: statistics.median(member[field] for member in complete if member.get(field) is not None) for field in fields if any(member.get(field) is not None for member in complete)}
            totals = [member["totals"] for member in complete]
            record["median_totals"] = {field: statistics.median(total[field] for total in totals) for field in ("raw_bytes", "container_bytes", "encoded_payload_bytes", "effective_payload_bytes", "header_and_index_bytes", "container_payload_bytes", "kept_raw_files", "raw_chunks", "compressed_chunks", "range_probe_payload_bytes_read", "range_probe_raw_bytes_decoded", "range_probe_direct_bytes_read")}
            record["median_saving_fraction"] = statistics.median((total["raw_bytes"] - total["container_bytes"]) / total["raw_bytes"] if total["raw_bytes"] else 0 for total in totals)
        output.append(record)
    return {"schema_version": SCRIPT_VERSION, "trial_count": len(rows), "completed_count": sum(row.get("status") == "complete" for row in rows), "skipped_or_failed_count": sum(row.get("status") != "complete" for row in rows), "configurations": output}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="schema-version-1 artifact manifest")
    parser.add_argument("--corpus", type=Path, help="root for manifest relative_path values; defaults to manifest parent")
    parser.add_argument("--out", type=Path, help="new output directory for run record, logs, and trial JSONL")
    parser.add_argument("--profiles", nargs="+", choices=("legacy", "fast", "balanced", "small", "screen", "incremental"), default=["screen"], help="named sets of codec/parameter combinations (ignored when --codec-spec is supplied)")
    parser.add_argument("--codec-spec", nargs="+", metavar="METHOD=PARAMETER", help="run exact codec candidates, for example zstd=-1 zstd=3 lz4=fast:1")
    parser.add_argument("--chunk-sizes", nargs="+", choices=("whole", "16KiB", "64KiB", "256KiB", "1MiB", "4MiB"), default=["whole", "64KiB", "256KiB", "1MiB"])
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20260926)
    parser.add_argument("--artifact-class", nargs="+", help="optional inferred artifact classes to include (for example dep-graph query-cache object rmeta)")
    parser.add_argument("--limit", type=int, help="limit artifacts after lexical path sorting; for smoke tests only")
    parser.add_argument("--max-configs", type=int, help="limit configurations after profile expansion; for smoke tests only")
    parser.add_argument("--timeout-seconds", type=float, default=300, help="per-config worker timeout; timed-out trials are recorded and the sweep continues")
    parser.add_argument("--list-codecs", action="store_true", help="print locally available libraries without reading or benchmarking a corpus")
    parser.add_argument("--_worker-manifest", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_worker-selection", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--_worker-config", help=argparse.SUPPRESS)
    parser.add_argument("--_worker-result", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args._worker_manifest:
        config = json.loads(args._worker_config)
        run_worker(args._worker_manifest, args._worker_root, args._worker_selection, config, args._worker_result)
        return 0
    if args.list_codecs:
        print(json.dumps({"libraries": available_methods(), "profiles": {name: [spec.key for spec in specs_for(name)] for name in ("legacy", "fast", "balanced", "small", "screen", "incremental")}}, indent=2))
        return 0
    if args.manifest is None or args.out is None:
        raise SystemExit("--manifest and --out are required")
    if args.repetitions < 1:
        raise SystemExit("--repetitions must be >= 1")
    return run_parent(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        raise
