"""VOD 分片规划与播放列表生成单测（docs/design/web-player.md §12）。

分片边界规则必须与 ffmpeg hls muxer 一致（超过目标时长后的第一个关键帧切），
这里锁死规则本身；与真实 ffmpeg 的吻合由集成冒烟验证。
"""

from __future__ import annotations

from movieclaw_playback.hls_vod import (
    build_master_playlist,
    build_media_playlist,
    build_subtitle_playlist,
    compute_segment_plan,
)

# 1 秒一个关键帧的理想片源
KF_1S = tuple(float(i) for i in range(0, 30))


def test_boundaries_follow_ffmpeg_rule():
    """目标 4 秒 → 每 4 个关键帧切一段；边界都落在关键帧上。"""
    plan = compute_segment_plan(KF_1S, 30.0, target_s=4.0)
    assert plan.boundaries == (0.0, 4.0, 8.0, 12.0, 16.0, 20.0, 24.0, 28.0)
    assert plan.duration_of(0) == 4.0
    assert plan.duration_of(plan.count - 1) == 2.0  # 末段吃掉余量


def test_irregular_keyframes_follow_absolute_grid():
    """ffmpeg 的切分目标是绝对栅格 k×target：栅格 4→切 5.1，栅格 8→切 8.0，
    栅格 12→切 12.5（若上一段超长，栅格不按切点顺延——这是与「起点+target」
    规则的分叉点，实测以 ffmpeg 为准）。"""
    plan = compute_segment_plan((0.0, 1.2, 3.9, 5.1, 8.0, 9.9, 12.5), 14.5, target_s=4.0)
    assert plan.boundaries == (0.0, 5.1, 8.0, 12.5)


def test_tiny_tail_merges_into_last_segment():
    """片尾不足半个目标的尾巴并入前一段，不单独成段。"""
    plan = compute_segment_plan((0.0, 4.0, 8.0), 9.0, target_s=4.0)
    assert plan.boundaries == (0.0, 4.0)
    assert plan.duration_of(1) == 5.0


def test_segment_for_position():
    plan = compute_segment_plan(KF_1S, 30.0, target_s=4.0)
    assert plan.segment_for(0) == 0
    assert plan.segment_for(3.999) == 0
    assert plan.segment_for(4.0) == 1
    assert plan.segment_for(29.9) == plan.count - 1
    assert plan.segment_for(999) == plan.count - 1  # 越界钳到末段


def test_media_playlist_is_vod_with_endlist():
    plan = compute_segment_plan(KF_1S, 30.0, target_s=4.0)
    text = build_media_playlist(
        plan, init_name="init.mp4", segment_name="seg%05d.m4s", query="?token=t1"
    )
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in text
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
    assert '#EXT-X-MAP:URI="init.mp4?token=t1"' in text
    assert "seg00000.m4s?token=t1" in text
    assert "seg00007.m4s?token=t1" in text
    # EXTINF 总和 = 片长（播放器据此显示总时长与 seek）
    total = sum(
        float(line[len("#EXTINF:"):-1])
        for line in text.splitlines()
        if line.startswith("#EXTINF:")
    )
    assert abs(total - 30.0) < 0.001


def test_master_playlist_carries_subtitle_group():
    text = build_master_playlist(
        media_uri="media.m3u8",
        subtitles=[("中文", "sub0.m3u8"), ("英文", "sub1.m3u8")],
        query="?token=t1",
    )
    assert 'TYPE=SUBTITLES,GROUP-ID="subs",NAME="中文",DEFAULT=YES' in text
    assert 'NAME="英文",DEFAULT=NO' in text
    assert 'SUBTITLES="subs"' in text
    assert "media.m3u8?token=t1" in text


def test_master_playlist_without_subtitles():
    text = build_master_playlist(media_uri="media.m3u8")
    assert "SUBTITLES" not in text
    assert "#EXT-X-STREAM-INF" in text


def test_master_playlist_carries_exact_codecs():
    text = build_master_playlist(
        media_uri="media.m3u8",
        codecs="avc1.640029,mp4a.40.2",
    )
    assert '#EXT-X-STREAM-INF:BANDWIDTH=80000000,CODECS="avc1.640029,mp4a.40.2"' in text


def test_subtitle_playlist_single_segment():
    text = build_subtitle_playlist(vtt_uri="sub.vtt", duration_s=1800.5, query="?token=t")
    assert "#EXTINF:1800.500000," in text
    assert "sub.vtt?token=t" in text
    assert text.rstrip().endswith("#EXT-X-ENDLIST")
