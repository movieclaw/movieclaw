"""MP4 里 HEVC 样本条目的标签修正：直出时把 ``hev1`` 改成 ``hvc1``。

背景（issue #430）：WebKit 只认 ``hvc1`` 标签的 HEVC MP4，``hev1`` 一律
``MEDIA_ERR_SRC_NOT_SUPPORTED``；而国内 WEB-DL 组出的 MP4 几乎全是 ``hev1``
（现场一个库里 527 个 HEVC MP4 有 416 个）。两者的差别只是 ``stsd`` 表里
样本条目那 4 个字节的类型码——码流一个比特都不动——所以不必重封装、也
不必碰用户的文件：档 0 直出的 Range 响应流经这 4 个字节时原地替换即可，
零 IO 零转码。

只在 ``hvcC`` 自带参数集（``numOfArrays > 0``）时才改：``hvc1`` 的语义是
"参数集在 hvcC 里"，参数集只在带内的 ``hev1`` 改了标签反而会让严格的解码器
找不到 SPS/PPS。实际见到的 ``hev1`` 文件 hvcC 基本都带参数集（ffmpeg mov
muxer 一律会写），这道闸是防极端情况。

解析只读盒子头，逐层下钻 ``moov → trak → mdia → minf → stbl → stsd``，
mdat 直接按尺寸跳过；moov 在文件尾（非 faststart）也只是多几次 seek。
任何结构不对劲就放弃返回空——宁可不改，也不能把错的字节发给播放器。
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

logger = logging.getLogger("movieclaw_playback.mp4_sample_entry")

#: 一个补丁 = (文件内偏移, 要写入的字节)。当前只有 4 字节的类型码替换。
BytePatch = tuple[int, bytes]

_CONTAINER_BOXES = {b"moov", b"trak", b"mdia", b"minf", b"stbl"}
#: stsd 整箱读入的上限——正常几百字节，超过说明文件不对劲
_STSD_MAX_BYTES = 1 << 20
#: VisualSampleEntry 固定字段长度（盒子头 8 + SampleEntry 8 + 视觉字段 70），
#: 之后才是 hvcC / pasp / colr 等子盒子
_VISUAL_SAMPLE_ENTRY_HEADER = 86
#: hvcC 载荷里 numOfArrays 的偏移（ISO/IEC 14496-15 8.3.3.1.2，前 22 字节是
#: profile/level/chroma/bit depth 等定长字段）
_HVCC_NUM_OF_ARRAYS_OFFSET = 22

#: (路径, mtime_ns, 大小) → 补丁。同 keyframes 的取舍：满了整体清空。
_patch_cache: dict[tuple[str, int, int], tuple[BytePatch, ...]] = {}
_PATCH_CACHE_MAX = 256


def hev1_to_hvc1_patches(path: str | Path) -> tuple[BytePatch, ...]:
    """返回把文件里所有 ``hev1`` 样本条目改成 ``hvc1`` 需要的字节补丁。

    不是 MP4、没有 ``hev1`` 条目、或结构解析失败都返回空元组；结果按
    (路径, mtime, 大小) 缓存，文件被换掉时自然失效。
    """
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return ()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _patch_cache.get(key)
    if cached is not None:
        return cached
    try:
        with path.open("rb") as fh:
            patches = tuple(_find_hev1_entries(fh, stat.st_size))
    except (OSError, struct.error, ValueError) as exc:
        logger.warning("解析 MP4 样本条目失败，直出不改标签：%s（%s）", path, exc)
        return ()
    if len(_patch_cache) >= _PATCH_CACHE_MAX:
        _patch_cache.clear()
    _patch_cache[key] = patches
    return patches


def apply_byte_patches(chunk: bytes, offset: int, patches: tuple[BytePatch, ...]) -> bytes:
    """把与 ``chunk``（位于文件偏移 ``offset``）重叠的补丁写进去。

    补丁可能跨块边界——只写落在本块内的那一段。无重叠时原样返回，不复制。
    """
    out: bytearray | None = None
    end = offset + len(chunk)
    for at, data in patches:
        if at >= end or at + len(data) <= offset:
            continue
        if out is None:
            out = bytearray(chunk)
        lo = max(at, offset)
        hi = min(at + len(data), end)
        out[lo - offset : hi - offset] = data[lo - at : hi - at]
    return bytes(out) if out is not None else chunk


def _read_box_header(fh, pos: int, limit: int) -> tuple[int, bytes, int] | None:
    """读 ``pos`` 处的盒子头，返回 (盒子总长, 类型, 载荷起点)；到界返回 None。"""
    if pos + 8 > limit:
        return None
    fh.seek(pos)
    head = fh.read(8)
    if len(head) < 8:
        return None
    size, kind = struct.unpack(">I4s", head)
    payload = pos + 8
    if size == 1:
        size = struct.unpack(">Q", fh.read(8))[0]
        payload = pos + 16
    elif size == 0:
        size = limit - pos
    if size < payload - pos or pos + size > limit:
        raise ValueError(f"盒子 {kind!r} 尺寸越界 @{pos}")
    return size, kind, payload


def _iter_boxes(fh, start: int, end: int):
    pos = start
    while pos < end:
        box = _read_box_header(fh, pos, end)
        if box is None:
            return
        size, kind, payload = box
        yield pos, size, kind, payload
        pos += size


def _find_hev1_entries(fh, file_size: int):
    """深度优先找到每个 ``stsd``，交给 ``_scan_stsd`` 检查样本条目。"""
    saw_ftyp = False
    stack = [(0, file_size)]
    while stack:
        start, end = stack.pop()
        for pos, size, kind, payload in _iter_boxes(fh, start, end):
            if kind == b"ftyp":
                saw_ftyp = True
            elif kind in _CONTAINER_BOXES:
                stack.append((payload, pos + size))
            elif kind == b"stsd":
                yield from _scan_stsd(fh, pos, size, payload)
        if not saw_ftyp and start == 0:
            # 顶层没有 ftyp 就不是 ISO BMFF，别再往里猜
            return


def _scan_stsd(fh, box_pos: int, box_size: int, payload: int):
    if box_size > _STSD_MAX_BYTES:
        raise ValueError(f"stsd 过大（{box_size} 字节）")
    fh.seek(payload)
    body = fh.read(box_pos + box_size - payload)
    if len(body) < 8:
        raise ValueError("stsd 头不完整")
    entry_count = struct.unpack(">I", body[4:8])[0]
    cursor = 8
    for _ in range(entry_count):
        if cursor + 8 > len(body):
            raise ValueError("stsd 条目数与内容不符")
        entry_size, entry_kind = struct.unpack(">I4s", body[cursor : cursor + 8])
        if entry_size < 8 or cursor + entry_size > len(body):
            raise ValueError(f"样本条目 {entry_kind!r} 尺寸越界")
        entry = body[cursor : cursor + entry_size]
        if entry_kind == b"hev1" and _hvcc_carries_parameter_sets(entry):
            # 类型码紧跟在 4 字节尺寸之后
            yield (payload + cursor + 4, b"hvc1")
        cursor += entry_size


def _hvcc_carries_parameter_sets(entry: bytes) -> bool:
    """样本条目的 hvcC 里 numOfArrays > 0 才允许改标签（见模块说明）。"""
    cursor = _VISUAL_SAMPLE_ENTRY_HEADER
    while cursor + 8 <= len(entry):
        size, kind = struct.unpack(">I4s", entry[cursor : cursor + 8])
        if size < 8 or cursor + size > len(entry):
            return False
        if kind == b"hvcC":
            at = cursor + 8 + _HVCC_NUM_OF_ARRAYS_OFFSET
            return at < cursor + size and entry[at] > 0
        cursor += size
    return False
