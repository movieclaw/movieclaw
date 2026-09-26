import CoreImage
import ImageIO
import SwiftUI
import UIKit

/// 全站背景大图 + 背景蒙版（银玻璃主题的「底」，对应 Web lib/backdrop.tsx 与 lib/ui-prefs.tsx 的蒙版部分）。
///
/// Web 手机端的每个页面都叠在同一套底上（globals.css）：
/// 1. `body::before`：用户选定的背景图（`GET /appearance` 的 active_url，空 = 内置 `/backdrop-default.jpg`），
///    cover + 顶部对齐，上面再压三团冷色径向光晕与一层纵向渐暗；
/// 2. `.page-scrim`：全屏模糊 + 压暗蒙版，模糊半径与暗度来自 `ui.preferences.scrim`
///    （设置 → 外观 → 界面质感的两根滑杆，默认 13px / 0.69）。
/// 「侧栏透明度/明暗/厚度」只作用于网页桌面端的侧栏玻璃，手机端没有侧栏，App 同样不读。
///
/// App 的做法：这里一个全局单例持有「当前背景图 + 蒙版参数」，每个页面的 `.appBackground()` 画同一份，
/// 外观页上传 / 切换 / 删除背景、拖动或保存质感滑杆后调用 `apply…` 即时生效，全 App 跟随。
/// 首帧优化同 Web 的 localStorage 缓存：上次的背景缩略图与蒙版参数落在本机缓存，冷启动先画缓存，
/// 接口回来后再以服务端为准纠正。
///
/// 例外（与 Web 一致）：影片详情类「氛围页」自带沉浸大图、不铺蒙版，用 `.appBackground(.plain)`；
/// AI 会话页是不透明纯色底，由会话模块自己画。
@MainActor
@Observable
final class AppBackdropStore {
    static let shared = AppBackdropStore()

    /// 蒙版参数（Web ScrimUiPrefs）
    struct Scrim: Equatable {
        /// 高斯模糊半径（pt，对应 CSS px）
        var blur: Double
        /// 压暗程度 0~1
        var dark: Double
        /// 出厂默认（与 Web DEFAULT_UI_PREFS、后端 ScrimUiPrefs 一致）
        static let defaults = Scrim(blur: 13, dark: 0.69)
    }

    /// 已解码、缩到屏幕尺寸的背景图；nil 时只画底色与光晕
    private(set) var image: UIImage?
    /// 已保存的蒙版参数
    private(set) var savedScrim: Scrim
    /// 外观页拖动滑杆时的预览草稿（未落库）；离开外观页撤销
    var previewScrim: Scrim?
    /// 实际生效的蒙版参数（有预览用预览）
    var scrim: Scrim { previewScrim ?? savedScrim }

    /// 当前图对应的地址（去重用：同一张图不重复下载）
    private var imageKey: String?
    /// 按「画布尺寸 + 模糊半径」缓存的模糊成品（见 blurred(for:)）；换图时清空
    private var blurCache: [BlurKey: UIImage] = [:]
    /// 每个画布尺寸最近一次的成品：拖动滑杆时新半径还没算完，先显示上一张，不闪底色
    private var lastBlurred: [CGSize: UIImage] = [:]

    struct BlurKey: Hashable {
        var width: Int
        var height: Int
        /// 半径按 0.5pt 取整，拖动滑杆时不至于每个像素都重算
        var halfPoints: Int
    }

    private static let cacheKey = "movieclaw.backdrop.cacheKey"
    private static let scrimKey = "movieclaw.backdrop.scrim"
    /// 缩略边长：够铺满手机全屏（模糊后细节本就看不出），又不至于常驻一张 2560px 大图
    private nonisolated static let maxPixel: CGFloat = 1600

    private init() {
        let defaults = UserDefaults.standard
        if let values = defaults.array(forKey: Self.scrimKey) as? [Double], values.count == 2 {
            savedScrim = Scrim(blur: values[0], dark: values[1])
        } else {
            savedScrim = .defaults
        }
        if let key = defaults.string(forKey: Self.cacheKey),
           let data = try? Data(contentsOf: Self.cacheFile),
           let cached = UIImage(data: data) {
            image = cached
            imageKey = key
        }
    }

    // MARK: 拉取与回写

    /// 进入主界面（含切换账号后重建）时拉一次：当前账号的背景图 + 界面偏好里的蒙版参数。
    /// 未登录（登录页）只拉背景图——后端此时返回管理员为登录页设的全局背景。
    /// 失败不致命：沿用缓存/内置默认（同 Web「读取外观设置失败，暂用内置默认背景」）。
    func refresh(api: APIClient, includePrefs: Bool) async {
        if includePrefs, let prefs = try? await api.uiPrefsShow() {
            apply(prefs: prefs)
        }
        if let view = try? await api.appearanceShow() {
            await apply(appearance: view, api: api)
        }
    }

    /// 外观页每次背景图写操作都拿到后端最新视图，整体回写（同 Web applyView）
    func apply(appearance view: API.AppearanceView, api: APIClient) async {
        guard let url = view.activeUrl.flatMap({ api.image($0) }) ?? api.server.resolve("/backdrop-default.jpg") else { return }
        let key = url.absoluteString
        guard key != imageKey else { return }
        guard let (data, response) = try? await api.session.data(from: url),
              (response as? HTTPURLResponse).map({ (200 ..< 300).contains($0.statusCode) }) ?? false,
              let thumbnail = await Self.downscale(data)
        else { return }
        image = thumbnail.image
        imageKey = key
        blurCache = [:]
        lastBlurred = [:]
        UserDefaults.standard.set(key, forKey: Self.cacheKey)
        try? thumbnail.jpeg.write(to: Self.cacheFile, options: .atomic)
    }

    /// 界面偏好保存成功（或首次拉取）后回写已保存的蒙版参数，并撤销预览草稿
    func apply(prefs: API.UiPreferencesSetting) {
        savedScrim = Scrim(blur: prefs.scrim.blur, dark: prefs.scrim.dark)
        previewScrim = nil
        UserDefaults.standard.set([savedScrim.blur, savedScrim.dark], forKey: Self.scrimKey)
    }

    // MARK: 模糊成品

    /// 画布尺寸 + 模糊半径对应的成品；没有就返回同尺寸上一张（可能是旧半径 / 旧图），都没有返回 nil
    func blurred(for size: CGSize, blur: Double) -> UIImage? {
        blurCache[Self.key(size, blur)] ?? lastBlurred[size]
    }

    /// 按需生成模糊成品（后台线程）：先按 cover + 顶部对齐裁成画布比例、缩到 2 倍点数，再做高斯模糊。
    /// 不用 SwiftUI `.blur` 实时模糊：每个页面都要对整屏大图做一次大半径模糊，GPU 开销大，
    /// 模拟器上还会出现分块花屏；预先算好的静态图每页只是贴一张图。
    func prepareBlur(for size: CGSize, blur: Double) async {
        let key = Self.key(size, blur)
        guard size.width > 1, size.height > 1, blurCache[key] == nil, let image else { return }
        let source = image
        guard let result = await Self.render(source, canvas: size, blur: Double(key.halfPoints) / 2) else { return }
        // 算的过程中换了图：这张作废
        guard source === self.image else { return }
        blurCache[key] = result
        lastBlurred[size] = result
    }

    private static func key(_ size: CGSize, _ blur: Double) -> BlurKey {
        BlurKey(width: Int(size.width.rounded()), height: Int(size.height.rounded()), halfPoints: Int((max(0, blur) * 2).rounded()))
    }

    private nonisolated static let ciContext = CIContext(options: [.cacheIntermediates: false])

    private nonisolated static func render(_ image: UIImage, canvas: CGSize, blur: Double) async -> UIImage? {
        await Task.detached(priority: .userInitiated) {
            // 1) cover + 顶部对齐（Web background: center top / cover），按 2 倍点数出图
            let scale: CGFloat = 2
            let pixel = CGSize(width: (canvas.width * scale).rounded(), height: (canvas.height * scale).rounded())
            let fit = max(pixel.width / image.size.width, pixel.height / image.size.height)
            let drawn = CGSize(width: image.size.width * fit, height: image.size.height * fit)
            let format = UIGraphicsImageRendererFormat()
            format.scale = 1
            format.opaque = true
            let base = UIGraphicsImageRenderer(size: pixel, format: format).image { _ in
                image.draw(in: CGRect(x: (pixel.width - drawn.width) / 2, y: 0, width: drawn.width, height: drawn.height))
            }
            guard blur > 0, let input = CIImage(image: base) else { return base }
            // 2) 高斯模糊：边缘先无限延展再裁回原尺寸，四周不会渗进黑边（同 CSS backdrop-filter 的观感）
            let blurred = input.clampedToExtent()
                .applyingGaussianBlur(sigma: blur * scale)
                .cropped(to: input.extent)
            guard let cg = ciContext.createCGImage(blurred, from: input.extent) else { return base }
            return UIImage(cgImage: cg)
        }.value
    }

    // MARK: 缓存与解码

    private static var cacheFile: URL {
        URL.cachesDirectory.appending(path: "app-backdrop.jpg")
    }

    /// 后台解码并缩到 maxPixel：ImageIO 直接出缩略图，不先解出整张原图
    private nonisolated static func downscale(_ data: Data) async -> (image: UIImage, jpeg: Data)? {
        await Task.detached(priority: .utility) {
            guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
            let options: [CFString: Any] = [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: maxPixel,
            ]
            guard let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
            let image = UIImage(cgImage: cg)
            guard let jpeg = image.jpegData(compressionQuality: 0.85) else { return nil }
            return (image, jpeg)
        }.value
    }
}

/// 页面底的三种画法
enum AppBackdropStyle {
    /// 默认：背景图 + 光晕 + 模糊压暗蒙版（Web 全站 .page-scrim）
    case scrim
    /// 登录页：背景图直出，只轻压一层暗色托住表单（Web AuthScreen 的壁纸上没有全局蒙版）
    case sharp
    /// 氛围页（影片详情）：纯深色底，页面自己铺沉浸大图（Web 的 isHome 页不铺蒙版、由详情页换背景）
    case plain
}

/// 背景层本体（由 `.appBackground()` 铺在页面最底下）
struct AppBackdropView: View {
    var style: AppBackdropStyle = .scrim
    private var store: AppBackdropStore { .shared }

    var body: some View {
        switch style {
        case .plain:
            Theme.background
                .overlay {
                    LinearGradient(
                        colors: [Color(red: 0.10, green: 0.12, blue: 0.18).opacity(0.9), .clear],
                        startPoint: .top, endPoint: .center
                    )
                }
                .ignoresSafeArea()
        case .sharp:
            AppBackdropCanvas(blur: 0)
                .overlay(Color(red: 5 / 255, green: 7 / 255, blue: 12 / 255).opacity(0.35))
                .ignoresSafeArea()
        case .scrim:
            let scrim = store.scrim
            AppBackdropCanvas(blur: scrim.blur)
                // .page-scrim 的底色 rgb(5 7 12 / dark)
                .overlay(Color(red: 5 / 255, green: 7 / 255, blue: 12 / 255).opacity(scrim.dark))
                .ignoresSafeArea()
        }
    }
}

/// 背景画布：Web body::before 的各层——底色、背景图（cover、顶部对齐，已按需模糊）、纵向渐暗、三团冷色径向光晕。
private struct AppBackdropCanvas: View {
    let blur: Double
    private var store: AppBackdropStore { .shared }

    var body: some View {
        GeometryReader { proxy in
            let size = proxy.size
            ZStack {
                Theme.background
                if let image = store.blurred(for: size, blur: blur) {
                    Image(uiImage: image)
                        .resizable()
                        .frame(width: size.width, height: size.height)
                }
                LinearGradient(
                    colors: [Color(red: 9 / 255, green: 11 / 255, blue: 17 / 255).opacity(0.1),
                             Color(red: 6 / 255, green: 7 / 255, blue: 13 / 255).opacity(0.32)],
                    startPoint: .top, endPoint: .bottom
                )
                glow(Color(red: 64 / 255, green: 150 / 255, blue: 186 / 255).opacity(0.13), at: UnitPoint(x: 0.80, y: 0.92), size: size, rx: 0.64, ry: 0.58)
                glow(Color(red: 140 / 255, green: 104 / 255, blue: 214 / 255).opacity(0.14), at: UnitPoint(x: 0.88, y: 0.14), size: size, rx: 0.54, ry: 0.44)
                glow(Color(red: 96 / 255, green: 130 / 255, blue: 220 / 255).opacity(0.18), at: UnitPoint(x: 0.16, y: 0.08), size: size, rx: 0.58, ry: 0.48)
            }
            .frame(width: size.width, height: size.height)
            .clipped()
            // 尺寸、半径或背景图变化时生成对应成品（已有缓存则立即返回）
            .task(id: TaskKey(size: size, blur: blur, image: store.image.map(ObjectIdentifier.init))) {
                await store.prepareBlur(for: size, blur: blur)
            }
        }
    }

    private struct TaskKey: Equatable {
        var size: CGSize
        var blur: Double
        var image: ObjectIdentifier?
    }

    /// CSS `radial-gradient(rx% ry% at x% y%, color, transparent ~62%)` 的近似：椭圆径向渐变
    private func glow(_ color: Color, at center: UnitPoint, size: CGSize, rx: CGFloat, ry: CGFloat) -> some View {
        EllipticalGradient(colors: [color, .clear], center: .center, startRadiusFraction: 0, endRadiusFraction: 0.62)
            .frame(width: size.width * rx * 2, height: size.height * ry * 2)
            .position(x: size.width * center.x, y: size.height * center.y)
    }
}
