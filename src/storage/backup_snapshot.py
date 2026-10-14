"""In-memory snapshot compression, authenticated encryption, and block deltas."""

from __future__ import annotations

import json
import os
import struct
import zlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BLOCK_SIZE = 4096
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024 * 1024
_HEADER = struct.Struct(">QI")
_BLOCK = struct.Struct(">QI")


def compress_full(snapshot: bytes) -> bytes:
    if not 100 <= len(snapshot) <= MAX_SNAPSHOT_BYTES:
        raise ValueError("지원하지 않는 snapshot 크기입니다.")
    return zlib.compress(snapshot)


def compress_delta(parent: bytes, snapshot: bytes) -> bytes:
    blocks = []
    for offset in range(0, len(snapshot), BLOCK_SIZE):
        block = snapshot[offset:offset + BLOCK_SIZE]
        if parent[offset:offset + len(block)] != block:
            blocks.append(_BLOCK.pack(offset, len(block)) + block)
    return zlib.compress(_HEADER.pack(len(snapshot), len(blocks)) + b"".join(blocks))


def _aad(manifest: dict) -> bytes:
    # Password wrappers can change independently; every snapshot/lineage field
    # remains authenticated, including unknown future fields.
    immutable = {key: value for key, value in manifest.items()
                 if key not in ("credentials", "database")}
    return json.dumps(immutable, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(",", ":")).encode("utf-8")


def _cipher(key: bytes) -> AESGCM:
    if len(key) != 32:
        raise ValueError("DB key는 32 bytes여야 합니다.")
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                   info=b"meal-delivery-v2-backup/snapshot/aesgcm/v2").derive(key)
    return AESGCM(derived)


def encrypt(compressed: bytes, key: bytes, manifest: dict) -> bytes:
    nonce = os.urandom(12)
    return nonce + _cipher(key).encrypt(nonce, compressed, _aad(manifest))


def decrypt(payload: bytes, key: bytes, manifest: dict, parent: bytes | None) -> bytes:
    compressed = _cipher(key).decrypt(payload[:12], payload[12:], _aad(manifest))
    size = manifest["snapshot"]["size"]
    limit = size if manifest["backup_kind"] == "full" else size + ((size + BLOCK_SIZE - 1) // BLOCK_SIZE) * _BLOCK.size + _HEADER.size
    decompressor = zlib.decompressobj()
    data = decompressor.decompress(compressed, limit + 1)
    if len(data) > limit or not decompressor.eof or decompressor.unused_data:
        raise ValueError("압축된 snapshot의 크기 또는 종료 정보가 올바르지 않습니다.")
    if manifest["backup_kind"] == "full":
        if parent is not None or len(data) != size:
            raise ValueError("전체 snapshot 크기가 올바르지 않습니다.")
        return data
    if parent is None or len(data) < _HEADER.size:
        raise ValueError("증분 snapshot의 부모가 없습니다.")
    result_size, count = _HEADER.unpack_from(data)
    if result_size != size or count > (size + BLOCK_SIZE - 1) // BLOCK_SIZE:
        raise ValueError("증분 snapshot 크기가 올바르지 않습니다.")
    result = bytearray(parent[:size])
    result.extend(b"\x00" * (size - len(result)))
    cursor = _HEADER.size
    previous_offset = -1
    for _ in range(count):
        if cursor + _BLOCK.size > len(data):
            raise ValueError("증분 block header가 잘렸습니다.")
        offset, length = _BLOCK.unpack_from(data, cursor)
        cursor += _BLOCK.size
        if (offset % BLOCK_SIZE or offset <= previous_offset or offset >= size
                or length != min(BLOCK_SIZE, size - offset)
                or cursor + length > len(data)):
            raise ValueError("증분 block 범위 또는 순서가 올바르지 않습니다.")
        result[offset:offset + length] = data[cursor:cursor + length]
        cursor += length
        previous_offset = offset
    if cursor != len(data):
        raise ValueError("증분 snapshot에 불필요한 데이터가 있습니다.")
    return bytes(result)
