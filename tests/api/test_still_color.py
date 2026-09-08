"""抓帧色彩链（issue #331 后续）：什么片该做色调映射、用哪条滤镜、失败怎么降级。

抓帧本身要真 ffmpeg，但**滤镜链的装配是纯函数**——和播放侧的命令装配同一个
分层原则（playback/ffmpeg_args 的模块文档），所以这些判断可以表驱动钉死，
不必每次都起进程。

这里守的是三条真机结论：
- Dolby Vision P5 的基础层是 IPT-PQ-c2，不做 DV 元数据还原就是「肤色发绿、
  头发发紫」；``tonemapx`` 的 ``apply_dovi`` 默认为 true，能救回来；
- SDR 片**绝不能**加 ``tonemapx``——会二次映射，画面整片压成青绿；
- 主图与章节场景图必须共用同一套决策，各写一遍迟早会漂。
"""

from __future__ import annotations

import pytest

from movieclaw_api.services.library.chapters import _filter_chains as chapter_chains
from movieclaw_api.services.library.thumbs import TONEMAP_FILTER, build_filter_chains
from movieclaw_api.services.media_probe import VideoColor, _dv_info

_SCALE = "scale='min(1280,iw)':-2"


def chains(color: VideoColor, **kw) -> list[list[str]]:
    return build_filter_chains(color, _SCALE, thumbnail="thumbnail=n=24", **kw)


# ---------------------------------------------------------------------------
# 探测：DOVI 配置记录的解析
# ---------------------------------------------------------------------------


def test_dv_info_reads_profile_and_compatibility():
    """ffprobe 的 side_data_list 形态（-show_streams 里就带，无需额外开关）。"""
    video = {
        "side_data_list": [
            {
                "side_data_type": "DOVI configuration record",
                "dv_profile": 5,
                "dv_bl_signal_compatibility_id": 0,
            }
        ]
    }
    assert _dv_info(video) == (5, False)


def test_dv_info_marks_backward_compatible_base_layer():
    """compatibility_id 非 0 = 基础层本身就能直接解读（P8.1 那一类）。"""
    video = {
        "side_data_list": [
            {
                "side_data_type": "DOVI configuration record",
                "dv_profile": 8,
                "dv_bl_signal_compatibility_id": 1,
            }
        ]
    }
    assert _dv_info(video) == (8, True)


@pytest.mark.parametrize(
    "name, video",
    [
        ("没有 side_data", {}),
        ("side_data 为 None", {"side_data_list": None}),
        ("有 side_data 但不是 DOVI", {"side_data_list": [{"side_data_type": "Display Matrix"}]}),
        ("元素不是 dict（ffprobe 输出异常）", {"side_data_list": ["junk"]}),
    ],
)
def test_dv_info_returns_none_for_non_dv(name, video):
    assert _dv_info(video) == (None, False), name


# ---------------------------------------------------------------------------
# 决策：什么片要做色调映射
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, color, tonemap, dv_metadata",
    [
        ("SDR", VideoColor(), False, False),
        ("HDR10", VideoColor(hdr="HDR10"), True, False),
        ("HLG", VideoColor(hdr="HLG"), True, False),
        (
            "DV P5（基础层是 IPT-PQ-c2）",
            VideoColor(hdr="Dolby Vision", dv_profile=5, dv_backward_compatible=False),
            True,
            True,
        ),
        (
            "DV P8.1（基础层向后兼容）",
            VideoColor(hdr="Dolby Vision", dv_profile=8, dv_backward_compatible=True),
            True,
            False,
        ),
    ],
)
def test_video_color_flags(name, color, tonemap, dv_metadata):
    assert color.needs_tonemap is tonemap, name
    assert color.needs_dv_metadata is dv_metadata, name


# ---------------------------------------------------------------------------
# 装配：滤镜链
# ---------------------------------------------------------------------------


def test_sdr_gets_no_tonemap_at_all():
    """喂 tonemapx 给 SDR 会二次映射：真机对照 (21,28,31) → (7,25,26)，
    红通道掉三分之二，画面整片压成青绿。这条是反向守护，不能松。"""
    result = chains(VideoColor())
    assert len(result) == 1
    assert not any("tonemapx" in f for f in result[0])
    assert result[0][-1] == "format=yuv420p"


def test_hdr_tries_tonemap_first_then_falls_back():
    """源码/裸机部署可能不是 jellyfin-ffmpeg，没有 tonemapx。
    HDR10/HLG 退回不映射画面偏灰但内容可辨，给降级链。"""
    result = chains(VideoColor(hdr="HDR10"))
    assert len(result) == 2
    assert TONEMAP_FILTER in result[0]
    assert not any("tonemapx" in f for f in result[1])


def test_dv_p5_has_no_fallback_chain():
    """DV P5 退回不映射不是「差一点」，是稳定产出一张肤色发绿、头发发紫的图。

    那看起来像片源坏了。宁可不出图让前端显示集号占位，也不要一张坏图——
    所以这里**只给映射链**，没有降级链。
    """
    result = chains(VideoColor(hdr="Dolby Vision", dv_profile=5))
    assert len(result) == 1
    assert TONEMAP_FILTER in result[0]


def test_backward_compatible_dv_keeps_fallback():
    """P8.1 的基础层是能直接解读的 HDR10，退回不映射只是偏灰，给降级链。"""
    result = chains(VideoColor(hdr="Dolby Vision", dv_profile=8, dv_backward_compatible=True))
    assert len(result) == 2


def test_tonemap_runs_after_scale():
    """真机实测：DOVI side data 能穿过 thumbnail 与 scale 存活，三种摆位出图
    逐像素一致，而放最后是在小图而不是 4K 原图上做变换，反倒最快
    （960ms vs 放最前 1143ms，比不做映射的基线 1048ms 还快）。"""
    chain = chains(VideoColor(hdr="HDR10"))[0]
    assert chain.index(_SCALE) < chain.index(TONEMAP_FILTER)


def test_tonemap_filter_carries_its_own_pixel_format():
    """tonemapx 自带 format=yuv420p，链尾不该再来一次多余的转换。"""
    chain = chains(VideoColor(hdr="HDR10"))[0]
    assert "format=yuv420p" in TONEMAP_FILTER
    assert chain.count("format=yuv420p") == 0  # 只在 tonemapx 参数里，不单列一节


def test_tonemap_matches_playback_chain():
    """截图与播放必须用同一个滤镜同一组参数，否则两处观感对不上。

    此前截图用的是上游 `tonemap=hable`（desat 默认 2），播放用的是
    `tonemapx=...:desat=0`，注释却写着「观感一致」。
    """
    from movieclaw_api.services.playback.ffmpeg_args import _SOFTWARE_TONEMAP

    assert TONEMAP_FILTER == _SOFTWARE_TONEMAP


def test_tail_is_appended_after_tonemap():
    """章节链要在最后挂 showinfo（thumbnail 选完帧它才只打印那一帧的 pts_time）。"""
    chain = chains(VideoColor(hdr="HDR10"), tail=("showinfo",))[0]
    assert chain[-1] == "showinfo"
    assert chain.index(TONEMAP_FILTER) < chain.index("showinfo")


def test_chapter_and_thumb_share_the_same_color_decision():
    """两处必须同源。差异只应在缩放宽度与 showinfo 上，色彩部分逐字相同。"""
    for color in (
        VideoColor(),
        VideoColor(hdr="HDR10"),
        VideoColor(hdr="Dolby Vision", dv_profile=5),
    ):
        thumb = chains(color)
        chapter = chapter_chains(color)
        assert len(thumb) == len(chapter), color
        for a, b in zip(thumb, chapter, strict=True):
            assert [f for f in a if "tonemapx" in f] == [f for f in b if "tonemapx" in f], color
