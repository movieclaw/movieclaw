"""合成最小可用的 HDMV PGS（.sup）字幕流——测试专用。

ffmpeg **没有 PGS 编码器**（只能解码/流拷贝），要造带 PGS 轨的测试样本只能
按段格式手工拼字节。这里生成「一条白色实心矩形字幕，start_s 出现、end_s
消失」的最小合法流：显示集（PCS+WDS+PDS+ODS+END）+ 清除集（PCS+WDS+END）。

段格式（蓝光 HDMV PGS，网上有完整逆向文档，libbitsub/ffmpeg 的解析器同源）：
每段 = "PG" + PTS(u32, 90kHz) + DTS(u32, 恒 0) + 类型(u8) + 载荷长(u16) + 载荷。
类型：0x14 调色板 PDS / 0x15 位图 ODS / 0x16 合成 PCS / 0x17 窗口 WDS / 0x80 END。

位图 RLE：0x00 0x00 行结束；0x00 (0xC0|L高6位) L低8位 C = 长跑 L 个颜色 C。
调色板条目：id + YCrCb + Alpha；0 号透明是约定俗成（未定义即透明）。
"""

from __future__ import annotations

import struct


def _segment(seg_type: int, pts_s: float, payload: bytes) -> bytes:
    return (
        b"PG"
        + struct.pack(">IIBH", int(round(pts_s * 90000)), 0, seg_type, len(payload))
        + payload
    )


def make_sup(
    *,
    video_w: int = 640,
    video_h: int = 360,
    x: int = 170,
    y: int = 280,
    w: int = 300,
    h: int = 50,
    start_s: float = 1.0,
    end_s: float = 4.0,
) -> bytes:
    """一条「白色实心矩形」字幕：位于 (x, y)、大小 w×h，[start_s, end_s) 显示。"""
    # 调色板 0 号（id, 版本）+ 1 号 = 不透明白（Y=235 Cr=Cb=128 A=255）
    pds = bytes([0, 0]) + bytes([1, 235, 128, 128, 255])
    # 每行：整行长跑颜色 1 + 行结束标记
    line = bytes([0x00, 0xC0 | (w >> 8), w & 0xFF, 0x01, 0x00, 0x00])
    rle = line * h
    ods = (
        struct.pack(">HBB", 0, 0, 0xC0)  # object_id, 版本, first+last
        + struct.pack(">I", len(rle) + 4)[1:]  # u24：宽高 4 字节 + RLE
        + struct.pack(">HH", w, h)
        + rle
    )
    wds = bytes([1, 0]) + struct.pack(">HHHH", x, y, w, h)
    pcs_show = (
        struct.pack(">HHB", video_w, video_h, 0x10)
        + struct.pack(">H", 0)  # composition_number
        + bytes([0x80, 0x00, 0x00, 1])  # epoch start, 无调色板更新, 调色板 0, 1 个对象
        + struct.pack(">HBBHH", 0, 0, 0x00, x, y)  # object 0 → window 0, 不裁剪
    )
    pcs_clear = (
        struct.pack(">HHB", video_w, video_h, 0x10)
        + struct.pack(">H", 1)
        + bytes([0x00, 0x00, 0x00, 0])  # normal case, 0 个对象 = 清屏
    )
    show = (
        _segment(0x16, start_s, pcs_show)
        + _segment(0x17, start_s, wds)
        + _segment(0x14, start_s, pds)
        + _segment(0x15, start_s, ods)
        + _segment(0x80, start_s, b"")
    )
    clear = (
        _segment(0x16, end_s, pcs_clear) + _segment(0x17, end_s, wds) + _segment(0x80, end_s, b"")
    )
    return show + clear
