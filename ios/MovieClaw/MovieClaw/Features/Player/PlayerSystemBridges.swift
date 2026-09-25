import AVKit
import MediaPlayer
import SwiftUI

/// 引擎渲染表面的宿主：把引擎的 UIView 原样塞进 SwiftUI（按引擎实例区分，换引擎即换视图）。
struct EngineSurface: UIViewRepresentable {
    let engineView: UIView

    func makeUIView(context: Context) -> UIView {
        let container = UIView()
        container.backgroundColor = .black
        attach(engineView, to: container)
        return container
    }

    func updateUIView(_ container: UIView, context: Context) {
        if engineView.superview !== container {
            container.subviews.forEach { $0.removeFromSuperview() }
            attach(engineView, to: container)
        }
    }

    private func attach(_ view: UIView, to container: UIView) {
        view.frame = container.bounds
        view.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        container.addSubview(view)
    }
}

/// 隔空播放（AirPlay）路由按钮：系统的 AVRoutePickerView
struct AirPlayButton: UIViewRepresentable {
    func makeUIView(context: Context) -> AVRoutePickerView {
        let picker = AVRoutePickerView()
        picker.tintColor = .white
        picker.activeTintColor = .systemBlue
        picker.prioritizesVideoDevices = true
        picker.accessibilityLabel = "隔空播放"
        return picker
    }

    func updateUIView(_ uiView: AVRoutePickerView, context: Context) {}
}

/// 系统音量：iOS 不允许直接改音量，唯一的公开途径是 MPVolumeView 里的滑杆。
/// 这个视图常驻在播放器里（几乎透明），同时也把系统音量 HUD 压掉，由我们自己的胶囊显示。
@MainActor
final class SystemVolume {
    static let shared = SystemVolume()
    let volumeView = MPVolumeView(frame: CGRect(x: -100, y: -100, width: 10, height: 10))

    private var slider: UISlider? { volumeView.subviews.compactMap { $0 as? UISlider }.first }

    /// 当前音量（0~1）
    var value: Float { AVAudioSession.sharedInstance().outputVolume }

    /// 能否由 App 调节（模拟器上没有真实的音量滑杆）
    var isAdjustable: Bool { slider != nil }

    func set(_ volume: Float) {
        slider?.setValue(min(1, max(0, volume)), animated: false)
        slider?.sendActions(for: .valueChanged)
    }
}

struct SystemVolumeHost: UIViewRepresentable {
    func makeUIView(context: Context) -> MPVolumeView {
        let view = SystemVolume.shared.volumeView
        view.alpha = 0.01
        return view
    }

    func updateUIView(_ uiView: MPVolumeView, context: Context) {}
}

/// 界面方向：播放器的「横屏」键与退出时的归还
@MainActor
enum PlayerOrientation {
    static func request(landscape: Bool) {
        guard let scene = UIApplication.shared.connectedScenes.compactMap({ $0 as? UIWindowScene }).first else { return }
        let mask: UIInterfaceOrientationMask = landscape ? .landscapeRight : .portrait
        scene.keyWindow?.rootViewController?.setNeedsUpdateOfSupportedInterfaceOrientations()
        scene.requestGeometryUpdate(.iOS(interfaceOrientations: mask)) { _ in }
    }
}

/// 进度条缩略图：按需下载雪碧图并裁出目标格子（对应 Web `lib/player/trickplay.ts` 的 tileAt）。
@MainActor
@Observable
final class TrickplayImages {
    private var sheets: [String: UIImage] = [:]
    private var loading: Set<String> = []

    /// 文件时间（毫秒）→ 该显示的格子；雪碧图还没下完返回 nil（并开始下载）
    func tile(_ index: API.TrickplayView?, atMs ms: Int, resolve: (String) -> URL?, session: URLSession) -> UIImage? {
        guard let index, index.ready, index.count > 0, index.intervalMs > 0, index.columns > 0, index.rows > 0, !index.sheets.isEmpty else {
            return nil
        }
        let perSheet = index.columns * index.rows
        // 越界夹到最后一格：拖到片尾时给最后一帧，比忽然没有预览好
        let ordinal = min(max(0, ms) / index.intervalMs, index.count - 1)
        let sheetIndex = min(ordinal / perSheet, index.sheets.count - 1)
        let within = ordinal - sheetIndex * perSheet
        let path = index.sheets[sheetIndex]
        guard let sheet = sheets[path] else {
            load(path, url: resolve(path), session: session)
            return nil
        }
        let scale = sheet.scale
        let rect = CGRect(
            x: CGFloat((within % index.columns) * index.tileWidth) * scale,
            y: CGFloat((within / index.columns) * index.tileHeight) * scale,
            width: CGFloat(index.tileWidth) * scale,
            height: CGFloat(index.tileHeight) * scale
        )
        guard let cropped = sheet.cgImage?.cropping(to: rect) else { return nil }
        return UIImage(cgImage: cropped)
    }

    private func load(_ path: String, url: URL?, session: URLSession) {
        guard let url, !loading.contains(path) else { return }
        loading.insert(path)
        Task {
            if let (data, _) = try? await session.data(from: url), let image = UIImage(data: data) {
                sheets[path] = image
            }
            loading.remove(path)
        }
    }
}
