import XCTest

@testable import MovieClawTranscoder

/// 握手里申报给 NAS 的视频能力：Metal 滤镜与可硬解的编码（编码器解析见 WorkerConfigurationTests）。
final class VideoCapabilityProbeTests: XCTestCase {
    func testFiltersAreParsedWithoutTheLegend() {
        let output = """
        Filters:
          T.. = Timeline support
          .S. = Slice threading
          A = Audio input/output
         T. bwdif_videotoolbox V->V       BWDIF for VideoToolbox frames using Metal compute
         .. scale_vt          V->V       Scale Videotoolbox frames
         .S tonemap           V->V       Conversion to/from different dynamic ranges.
         .. tonemap_videotoolbox V->V       Perform HDR to SDR conversion with Metal.
        """
        let names = CapabilityProbe.parseFilters(output)
        XCTAssertEqual(names, ["bwdif_videotoolbox", "scale_vt", "tonemap", "tonemap_videotoolbox"])
        // 只申报 NAS 用得上的 Metal 滤镜
        XCTAssertEqual(
            names.filter(CapabilityProbe.metalFilters.contains),
            ["bwdif_videotoolbox", "scale_vt", "tonemap_videotoolbox"]
        )
    }

    func testFFmpegMajorVersionIsRead() {
        XCTAssertEqual(CapabilityProbe.majorVersion(of: "ffmpeg version 8.1.2-Jellyfin Copyright (c) 2000-2026"), 8)
        XCTAssertEqual(CapabilityProbe.majorVersion(of: "ffmpeg version n7.1 Copyright"), 7)
        XCTAssertNil(CapabilityProbe.majorVersion(of: "ffmpeg version N-112233-gabc Copyright"))
    }

    func testFourCharCodeMatchesCoreMedia() {
        XCTAssertEqual(CapabilityProbe.fourCharCode("avc1"), 0x6176_6331)
        XCTAssertEqual(CapabilityProbe.fourCharCode("hvc1"), 0x6876_6331)
    }

    func testHardwareDecodersAreKnownFFmpegCodecNames() {
        // 结果随机器而变（CI 的虚拟机可能一个都没有），只校验取值范围与 AV1 的版本门槛
        let names = Set(CapabilityProbe.hardwareDecodeCandidates.map(\.name))
        let found = CapabilityProbe.hardwareDecoders(ffmpegMajorVersion: 8)
        XCTAssertTrue(Set(found).isSubset(of: names))
        XCTAssertFalse(CapabilityProbe.hardwareDecoders(ffmpegMajorVersion: 7).contains("av1"))
        print("本机 VideoToolbox 可硬解：\(found)")
    }
}
