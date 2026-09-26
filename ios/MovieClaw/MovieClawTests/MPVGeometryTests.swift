import CoreGraphics
import Testing
@testable import MPVCore

/// MPV 画面区的纯计算（见 MPVMetalContainerView）：渲染面按视频比例居中摆放，
/// 横竖屏两端的画面区比例一致，旋转动画中间的每一帧才不会变形。
struct MPVGeometryTests {
    private let portrait = CGRect(x: 0, y: 0, width: 402, height: 874)
    private let landscape = CGRect(x: 0, y: 0, width: 874, height: 402)

    @Test func wideFilmFillsWidthInPortrait() {
        // 2.39:1 的片（1920x804）竖屏：宽度占满、上下居中
        let frame = MPVMetalContainerView.pictureFrame(video: CGSize(width: 1920, height: 804), in: portrait)
        #expect(frame == CGRect(x: 0, y: 353, width: 402, height: 168))
    }

    @Test func wideFilmFillsWidthInLandscape() {
        let frame = MPVMetalContainerView.pictureFrame(video: CGSize(width: 1920, height: 804), in: landscape)
        #expect(frame == CGRect(x: 0, y: 18, width: 874, height: 366))
    }

    @Test func sixteenByNineFillsHeightInLandscape() {
        // 16:9 的 4K 片横屏：高度占满、左右居中（屏幕比 16:9 更宽）
        let frame = MPVMetalContainerView.pictureFrame(video: CGSize(width: 3840, height: 2160), in: landscape)
        #expect(frame == CGRect(x: 80, y: 0, width: 715, height: 402))
    }

    @Test func unknownVideoSizeFillsContainer() {
        // 起播前、纯音频：还不知道比例，渲染面铺满，由 mpv 自己加黑边
        #expect(MPVMetalContainerView.pictureFrame(video: .zero, in: portrait) == portrait)
        #expect(MPVMetalContainerView.pictureFrame(video: CGSize(width: 1920, height: 1080), in: .zero) == .zero)
    }

    @Test func portraitAndLandscapeKeepVideoAspect() {
        // 取整到点之后，两端的画面区与视频比例的偏差都在 0.5% 以内：旧帧等比缩放时看不出变形
        let videos = [CGSize(width: 1920, height: 804), CGSize(width: 3840, height: 2160), CGSize(width: 1440, height: 1080), CGSize(width: 1080, height: 1920)]
        for video in videos {
            let aspect = video.width / video.height
            for bounds in [portrait, landscape] {
                let frame = MPVMetalContainerView.pictureFrame(video: video, in: bounds)
                #expect(abs(frame.width / frame.height / aspect - 1) < 0.005)
                #expect(bounds.contains(frame))
            }
        }
    }
}
