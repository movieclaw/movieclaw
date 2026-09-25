import SwiftUI

/// 按框比例铺图（对应 Web `PosterImage` + 「模糊铺底」手法）。
///
/// 海报墙每格的框比例是锁死的（竖 2:3 / 横 16:9），同一行等高、片名一条线；
/// 主图真实比例与框不一致时（4:3 封面、1.5 的横版海报），用同一张图放大模糊铺底、
/// 中央按真实比例完整显示一张清晰图，而不是裁掉或把格子撑变形。
struct LibraryArtwork: View {
    let url: URL?
    /// 主图真实宽高比；nil 视为与框一致
    var imageAspect: Double?
    /// 框的宽高比
    var frameAspect: CGFloat
    var placeholderSymbol: String = "film"
    /// 无图时在占位底上印的字（例如集号、片名）
    var fallbackText: String?

    var body: some View {
        Color.clear
            .aspectRatio(frameAspect, contentMode: .fit)
            .overlay {
                if url == nil, let fallbackText {
                    ZStack {
                        Theme.surfaceRaised
                        Text(fallbackText)
                            .font(.headline.weight(.bold))
                            .foregroundStyle(.white.opacity(0.25))
                            .multilineTextAlignment(.center)
                            .padding(12)
                    }
                } else if let aspect = imageAspect, abs(CGFloat(aspect) - frameAspect) > 0.05 {
                    ZStack {
                        RemoteImage(url: url, placeholderSymbol: placeholderSymbol)
                            .scaleEffect(1.25)
                            .blur(radius: 18)
                            .opacity(0.45)
                        Color.black.opacity(0.25)
                        RemoteImage(url: url, contentMode: .fit, placeholderSymbol: placeholderSymbol)
                            .aspectRatio(CGFloat(aspect), contentMode: .fit)
                            .shadow(color: .black.opacity(0.5), radius: 14)
                    }
                } else {
                    RemoteImage(url: url, placeholderSymbol: placeholderSymbol)
                }
            }
            .clipped()
    }
}

/// 媒体库海报格（对应 Web `InventoryCell` + `PosterCardVisual`）：海报 + 片名 + 年份。
///
/// 卡片下方只留片名与年份（副行可附加评分等「正在比较的指标」）；
/// 文件缺失是需要常显的异常，作为唯一例外在片名下单独点灯；
/// 文件全部缺失的「死条目」海报置灰；后台正在处理这一格时描边点亮并写出阶段。
struct LibraryPosterCell: View {
    let title: String
    var year: Int?
    /// 副行附加信息（按评分排序时的「★ 8.1」等）
    var extent: String?
    let url: URL?
    var imageAspect: Double?
    var frameAspect: CGFloat = Theme.posterAspect
    var favorite = false
    /// 右上角已看对勾
    var played = false
    /// 死条目：文件全部缺失
    var dead = false
    /// 片名下方的异常提示（如「2 个文件缺失」）
    var abnormal: String?
    /// 后台正在处理这一格的阶段文案
    var working: String?
    /// 海报左上角的角标（例如收藏层级、季集摘要）
    var cornerLabel: String?
    var placeholderSymbol = "film"
    /// 只压暗海报图、不压暗片名（合集里库中还没有的那几部）
    var artworkOpacity: Double = 1

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            LibraryArtwork(url: url, imageAspect: imageAspect, frameAspect: frameAspect, placeholderSymbol: placeholderSymbol)
                .clipShape(.rect(cornerRadius: Theme.posterRadius))
                .overlay(RoundedRectangle(cornerRadius: Theme.posterRadius).strokeBorder(Theme.line))
                .saturation(dead ? 0 : 1)
                .opacity(dead ? 0.5 : artworkOpacity)
                .overlay(alignment: .topTrailing) {
                    HStack(spacing: 4) {
                        if played {
                            Image(systemName: "checkmark")
                                .font(.caption2.weight(.bold))
                                .foregroundStyle(.white)
                                .frame(width: 22, height: 22)
                                .background(Theme.success.opacity(0.85), in: .circle)
                        }
                        if favorite {
                            Image(systemName: "heart.fill")
                                .font(.caption.weight(.semibold))
                                .foregroundStyle(Theme.danger)
                                .frame(width: 26, height: 22)
                                .background(.black.opacity(0.55), in: .rect(cornerRadius: 7))
                        }
                    }
                    .padding(7)
                }
                .overlay(alignment: .topLeading) {
                    if let cornerLabel {
                        Text(cornerLabel)
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.white.opacity(0.9))
                            .padding(.horizontal, 7).padding(.vertical, 3)
                            .background(.black.opacity(0.6), in: .capsule)
                            .padding(7)
                    }
                }
                .overlay {
                    if let working {
                        ZStack(alignment: .bottom) {
                            RoundedRectangle(cornerRadius: Theme.posterRadius).strokeBorder(Theme.info, lineWidth: 2)
                            HStack(spacing: 6) {
                                ProgressView().controlSize(.mini).tint(Theme.info)
                                Text(working).font(.caption2.weight(.medium)).lineLimit(1)
                            }
                            .foregroundStyle(Theme.info)
                            .padding(.horizontal, 8).padding(.vertical, 6)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(Color(red: 7 / 255, green: 12 / 255, blue: 20 / 255).opacity(0.92))
                            .clipShape(UnevenRoundedRectangle(bottomLeadingRadius: Theme.posterRadius, bottomTrailingRadius: Theme.posterRadius))
                        }
                    }
                }
            Text(title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(Theme.text)
                .lineLimit(1)
                .padding(.top, 8)
            let sub = [year.map(String.init), extent].compactMap { $0 }.joined(separator: " · ")
            Text(sub.isEmpty ? " " : sub)
                .font(.caption)
                .monospacedDigit()
                .foregroundStyle(Theme.textMuted)
                .lineLimit(1)
                .padding(.top, 1)
            if let abnormal {
                HStack(spacing: 5) {
                    Circle().fill(dead ? Color.white.opacity(0.3) : Theme.warning).frame(width: 5, height: 5)
                    Text(abnormal).lineLimit(1)
                }
                .font(.caption)
                .foregroundStyle(Theme.textMuted)
                .padding(.top, 3)
            }
        }
        .contentShape(.rect)
    }
}

/// 首页 / 详情页的分区头：标题 + 右侧入口（「查看全部」、⋯ 菜单）
struct LibrarySectionHeader<Trailing: View>: View {
    let title: String
    @ViewBuilder var trailing: () -> Trailing

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title)
                .font(.title3.weight(.semibold))
                .foregroundStyle(Theme.text)
                .lineLimit(1)
            Spacer(minLength: 12)
            trailing()
                .font(.subheadline)
                .foregroundStyle(Theme.textFaint)
        }
        .padding(.horizontal, Theme.pagePadding)
    }
}

extension LibrarySectionHeader where Trailing == EmptyView {
    init(title: String) {
        self.title = title
        trailing = { EmptyView() }
    }
}

/// 扫描 / 整理 / 元数据刷新共用的进度环（三者进度都是 已处理/总数；没有分母时转圈）
struct LibraryProgressRing: View {
    var processed: Int?
    var total: Int?
    var size: CGFloat = 72

    private var fraction: Double? {
        guard let processed, let total, total > 0 else { return nil }
        return min(1, Double(processed) / Double(total))
    }

    var body: some View {
        ZStack {
            Circle().stroke(.white.opacity(0.2), lineWidth: size * 0.07)
            if let fraction {
                Circle()
                    .trim(from: 0, to: fraction)
                    .stroke(.white, style: StrokeStyle(lineWidth: size * 0.07, lineCap: .round))
                    .rotationEffect(.degrees(-90))
                    .animation(.easeOut(duration: 0.5), value: fraction)
                Text("\(Int((fraction * 100).rounded()))%")
                    .font(.system(size: size * 0.2, weight: .semibold))
                    .monospacedDigit()
                    .foregroundStyle(.white)
            } else {
                ProgressView().tint(.white)
            }
        }
        .frame(width: size, height: size)
    }
}

/// 胶囊形筛选片（全部合集的来源/类型、首页自定义等）
struct LibraryChip: View {
    let title: String
    var selected: Bool
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(title)
                .font(.subheadline.weight(selected ? .semibold : .regular))
                .foregroundStyle(selected ? Color.black.opacity(0.85) : Theme.textMuted)
                .padding(.horizontal, 14)
                .padding(.vertical, 7)
                .background(selected ? AnyShapeStyle(Theme.accentStrong) : AnyShapeStyle(Theme.surfaceInset), in: .capsule)
                .overlay(Capsule().strokeBorder(selected ? .clear : Theme.line))
        }
        .buttonStyle(.plain)
    }
}
