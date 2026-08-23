"""播放列表分片地址补 token（docs/design/web-player.md §4.7）。

ffmpeg 写出来的地址是裸相对路径，浏览器按播放列表自身 URL 解析相对地址时会
把 query 丢掉——播放列表拿得到、分片全部没凭据。这里锁住「发出去的每个地址
都带 token」这条不变量。
"""

from __future__ import annotations

from movieclaw_api.api.routes.playback import playlist_with_tokens

# ffmpeg 实际产出的样子（-hls_segment_type fmp4 + EVENT 播放列表）
PLAYLIST = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:2
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXTINF:2.002000,
seg00000.m4s
#EXTINF:2.002000,
seg00001.m4s
"""


def test_init_and_segments_get_token() -> None:
    out = playlist_with_tokens(PLAYLIST, "abc.def")
    assert '#EXT-X-MAP:URI="init.mp4?token=abc.def"' in out
    assert "seg00000.m4s?token=abc.def" in out
    assert "seg00001.m4s?token=abc.def" in out


def test_tag_lines_untouched() -> None:
    out = playlist_with_tokens(PLAYLIST, "abc.def").splitlines()
    # 除 EXT-X-MAP 外的标签行不带地址，一个字都不该改
    assert out[0] == "#EXTM3U"
    assert out[4] == "#EXT-X-PLAYLIST-TYPE:EVENT"
    assert out[7] == "#EXTINF:2.002000,"


def test_token_appended_once_per_uri() -> None:
    out = playlist_with_tokens(PLAYLIST, "abc.def")
    assert out.count("token=") == 3  # init + 两个分片
    # 重复处理不该叠加（EVENT 播放列表每次请求都重新生成，但防呆）
    assert playlist_with_tokens(PLAYLIST, "abc.def").count("?token=") == 3


def test_empty_token_returns_playlist_as_is() -> None:
    assert playlist_with_tokens(PLAYLIST, "") == PLAYLIST


def test_line_endings_and_blank_lines_preserved() -> None:
    out = playlist_with_tokens("#EXTM3U\n\nseg00000.m4s\n", "t")
    assert out == "#EXTM3U\n\nseg00000.m4s?token=t\n"
