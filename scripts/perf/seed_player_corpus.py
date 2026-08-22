#!/usr/bin/env python3
"""黄金样本库：用 ffmpeg 合成覆盖各种病态形态的小片段（web-player.md §9.1）。

播放器最容易出事的地方不是常规片源，而是那些「看起来兼容、实际不行」的：
MKV 容器、HEVC、AC3/DTS、内封 ASS 与字体、长 GOP、开放 GOP、可变帧率。真实
片源不好共享（版权与体积），所以全部现场合成——每个几百 KB，可以放进任何环境。

用法::

    python scripts/perf/seed_player_corpus.py --out ./corpus
    # 然后把 ./corpus 作为媒体库根目录加进 movieclaw，扫描后逐个点开

每个样本的文件名就说明了它在验什么，播不了的那个名字直接指向对应的陷阱。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DURATION = 12
VIDEO = f"testsrc2=size=640x360:rate=25:duration={DURATION}"
AUDIO = f"sine=frequency=440:duration={DURATION}"

ASS_SUB = """[Script Info]
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour
Style: Karaoke,DejaVuSans,64,&H00FF00FF

[Events]
Format: Layer, Start, End, Style, Text
Dialogue: 0,0:00:00.50,0:00:06.00,Karaoke,{\\pos(320,300)}内封 ASS 特效字幕
Dialogue: 0,0:00:06.00,0:00:11.00,Karaoke,拖进度条应能看见缩略图
"""

SRT_SUB = """1
00:00:00,500 --> 00:00:06,000
内封文本字幕

2
00:00:06,000 --> 00:00:11,000
应转成 VTT 后渲染
"""


def run(argv: list[str]) -> None:
    proc = subprocess.run(argv, capture_output=True, timeout=300)
    if proc.returncode != 0:
        raise SystemExit(
            f"ffmpeg 失败：{' '.join(argv[:6])}…\n{proc.stderr.decode(errors='replace')[-800:]}"
        )


def base(video_codec: str, extra: list[str] | None = None) -> list[str]:
    encoder = {"h264": "libx264", "hevc": "libx265", "av1": "libsvtav1"}[video_codec]
    argv = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", VIDEO,
        "-f", "lavfi", "-i", AUDIO,
        "-c:v", encoder, "-preset", "ultrafast",
    ]
    if video_codec == "hevc":
        argv += ["-x265-params", "log-level=none"]
    argv += extra or []
    return argv


def build(out: Path) -> list[tuple[str, str]]:
    """返回 [(文件名, 这个样本在验什么)]。"""
    out.mkdir(parents=True, exist_ok=True)
    font = next(Path("/usr/share/fonts").rglob("*.ttf"), None)
    ass = out / ".sub.ass"
    srt = out / ".sub.srt"
    ass.write_text(ASS_SUB, encoding="utf-8")
    srt.write_text(SRT_SUB, encoding="utf-8")

    made: list[tuple[str, str]] = []

    def emit(name: str, argv: list[str], why: str) -> None:
        run([*argv, str(out / name)])
        made.append((name, why))

    emit(
        "01 直连 MP4 H264 AAC (2020).mp4",
        base("h264", ["-g", "50", "-c:a", "aac", "-shortest"]),
        "档 0：原文件直出，不该起任何会话",
    )
    emit(
        "02 直通 MKV H264 AAC (2020).mkv",
        base("h264", ["-g", "50", "-c:a", "aac", "-shortest"]),
        "档 1：容器 remux，码流不动",
    )
    emit(
        "03 音频单转 MKV HEVC AC3 5.1 (2020).mkv",
        base("hevc", ["-g", "50", "-c:a", "ac3", "-ac", "6", "-shortest"]),
        "档 2：视频直通、AC3 转码降混；Safari 上还验 hvc1 标签",
    )
    emit(
        "04 长GOP MKV H264 (2020).mkv",
        base("h264", ["-g", "500", "-keyint_min", "500", "-sc_threshold", "0",
                      "-c:a", "aac", "-shortest"]),
        "关键帧稀疏：决策应放弃 remux 改走转码（§7-②）",
    )
    emit(
        "05 可变帧率 MKV H264 (2020).mkv",
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", VIDEO,
            "-f", "lavfi", "-i", AUDIO,
            "-vf", "fps=fps=25,setpts=N/(25*TB)*if(gt(N\\,150)\\,1.5\\,1)",
            "-c:v", "libx264", "-preset", "ultrafast", "-g", "50",
            "-c:a", "aac", "-fps_mode", "vfr", "-shortest",
        ],
        "VFR：长片累积 A/V 漂移（§7-③b）",
    )

    argv = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", VIDEO,
        "-f", "lavfi", "-i", AUDIO,
        "-i", str(ass), "-i", str(srt),
    ]
    if font is not None:
        argv += ["-attach", str(font), "-metadata:s:t:0", "mimetype=application/x-truetype-font"]
    argv += [
        "-map", "0:v", "-map", "1:a", "-map", "2:s", "-map", "3:s",
        "-c:v", "libx265", "-preset", "ultrafast", "-x265-params", "log-level=none",
        "-g", "50", "-c:a", "aac", "-c:s:0", "ass", "-c:s:1", "srt", "-shortest",
    ]
    emit(
        "06 内封字幕与字体 MKV HEVC (2020).mkv",
        argv,
        "内封 ASS + 内嵌字体 + 内封 SRT：字体没抽出来时排版会明显走样",
    )

    ass.unlink(missing_ok=True)
    srt.unlink(missing_ok=True)
    return made


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="./corpus", help="样本输出目录")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None:
        print("需要系统 ffmpeg", file=sys.stderr)
        return 1

    out = Path(args.out)
    made = build(out)
    print(f"已生成 {len(made)} 个样本到 {out.resolve()}：\n")
    for name, why in made:
        print(f"  {name}\n      ↳ {why}")
    print("\n把这个目录当作电影库根路径加进 movieclaw，扫描后逐个点开验收。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
