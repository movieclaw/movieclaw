import SwiftUI
import UIKit

/// 播放画面上的触摸手势（对应 Web `lib/player/tap.ts`、`touch-adjust.ts`、`hold-speed.ts`）。
///
/// 用 UIKit 的原始触摸而不是 SwiftUI 手势组合：这里要在**同一根手指**上区分五种意图——
/// 单击、双击、横滑拖进度、竖滑调亮度/音量、长按倍速——而且单击必须**立即**生效
/// （不为等双击延迟 300 毫秒：点一下唤出控制条是最高频的交互）。SwiftUI 的手势优先级
/// 做不到「先按单击处理、第二下再改判双击」。
///
/// 判定规则：
/// - 手指移动不到 12pt 就抬起 = 轻点；距上次轻点 ≤ 300ms 且落在左右三分之一 = 双击跳转；
/// - 移动超过 12pt：横向为主 → 拖进度；纵向为主 → 左半屏亮度、右半屏音量；
/// - 按住 500ms 不动 = 长按 2 倍速（抬手恢复）；已进入倍速后的移动不再改判。暂停时不起长按，松手按轻点处理；
/// - 从屏幕上下边缘 32pt 内起手的触摸交给系统（控制中心/主屏幕手势），不当成播放器手势；
/// - 竖滑调节另有排除带（同 Web touch-adjust.ts）：顶部 12%、底部 24%（进度条与控制区）、左右各 32pt 起手不调；
///   滑过「高度 × 60%」从 0 拉满；
/// - 控制条、菜单所在的区域（`excludedRects`）整块不归手势层：`point(inside:)` 直接放行给上层按钮，
///   起手点落在里面也不跟踪——保证一次点按钮不会同时被当成「轻点画面」（开菜单的同时又把菜单/控制层收掉）。
struct PlayerGestureLayer: UIViewRepresentable {
    var enabled: Bool
    /// 现在能不能起长按倍速（暂停、锁屏时不能）
    var canHold: Bool
    /// 禁区（窗口坐标）：可见的控制条与菜单
    var excludedRects: [CGRect] = []
    var onTap: (_ xRatio: CGFloat, _ isDouble: Bool) -> Void
    var onScrub: (_ phase: GesturePhase, _ deltaRatio: CGFloat) -> Void
    var onAdjust: (_ phase: GesturePhase, _ side: AdjustSide, _ deltaRatio: CGFloat) -> Void
    var onHold: (_ began: Bool) -> Void

    enum GesturePhase { case began, changed, ended, cancelled }
    enum AdjustSide { case brightness, volume }

    func makeUIView(context: Context) -> GestureView {
        let view = GestureView()
        view.isMultipleTouchEnabled = false
        view.backgroundColor = .clear
        view.accessibilityIdentifier = "player-gesture-layer"
        return view
    }

    func updateUIView(_ view: GestureView, context: Context) {
        view.config = self
    }

    final class GestureView: UIView {
        var config: PlayerGestureLayer?

        private enum Intent { case undecided, scrub, adjust(AdjustSide), hold }
        private var start: CGPoint = .zero
        private var intent: Intent = .undecided
        private var holdTimer: Timer?
        private var lastTap: Date?
        private var tracking = false

        private static let activatePx: CGFloat = 12
        private static let edgeGuard: CGFloat = 32
        private static let doubleTapWindow: TimeInterval = 0.3
        private static let holdDelay: TimeInterval = 0.5
        /// 竖滑调节：满量程 = 可用高度 × 60%；起手排除带（比例 / pt）
        private static let fullSweepRatio: CGFloat = 0.6
        private static let adjustTopExclude: CGFloat = 0.12
        private static let adjustBottomExclude: CGFloat = 0.24

        /// 点在禁区里（控制条/菜单）：不认领这次触摸，让命中测试落到上层的 SwiftUI 按钮
        private func isExcluded(_ point: CGPoint) -> Bool {
            guard let rects = config?.excludedRects, !rects.isEmpty, window != nil else { return false }
            let inWindow = convert(point, to: nil)
            return rects.contains { $0.contains(inWindow) }
        }

        override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
            super.point(inside: point, with: event) && !isExcluded(point)
        }

        override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
            guard let config, config.enabled, let touch = touches.first, event?.allTouches?.count ?? 1 == 1 else { return }
            let point = touch.location(in: self)
            guard point.y > Self.edgeGuard, point.y < bounds.height - Self.edgeGuard, !isExcluded(point) else { return }
            tracking = true
            start = point
            intent = .undecided
            holdTimer?.invalidate()
            guard config.canHold else { return }
            holdTimer = Timer.scheduledTimer(withTimeInterval: Self.holdDelay, repeats: false) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self, self.tracking, case .undecided = self.intent else { return }
                    self.intent = .hold
                    self.config?.onHold(true)
                }
            }
        }

        override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
            guard tracking, let config, let touch = touches.first else { return }
            let point = touch.location(in: self)
            let dx = point.x - start.x, dy = point.y - start.y
            switch intent {
            case .undecided:
                guard max(abs(dx), abs(dy)) > Self.activatePx else { return }
                holdTimer?.invalidate()
                if abs(dx) >= abs(dy) {
                    intent = .scrub
                    config.onScrub(.began, 0)
                } else {
                    guard adjustAllowed(at: start) else {
                        // 排除带里起手的竖滑多半是想去摸进度条 / 下拉通知中心：整次触摸都不当手势
                        tracking = false
                        return
                    }
                    let side: AdjustSide = start.x < bounds.width / 2 ? .brightness : .volume
                    intent = .adjust(side)
                    config.onAdjust(.began, side, 0)
                }
            case .scrub:
                config.onScrub(.changed, dx / max(1, bounds.width))
            case let .adjust(side):
                // 上滑为增：屏幕坐标 y 向下，取负
                config.onAdjust(.changed, side, -dy / max(1, bounds.height * Self.fullSweepRatio))
            case .hold:
                break
            }
        }

        override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
            finish(cancelled: false, touch: touches.first)
        }

        override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
            finish(cancelled: true, touch: touches.first)
        }

        private func adjustAllowed(at point: CGPoint) -> Bool {
            point.y >= bounds.height * Self.adjustTopExclude
                && point.y <= bounds.height * (1 - Self.adjustBottomExclude)
                && point.x >= Self.edgeGuard && point.x <= bounds.width - Self.edgeGuard
        }

        private func finish(cancelled: Bool, touch: UITouch?) {
            holdTimer?.invalidate()
            guard tracking, let config else { return }
            tracking = false
            switch intent {
            case .undecided:
                guard !cancelled else { return }
                let x = (touch?.location(in: self).x ?? start.x) / max(1, bounds.width)
                let now = Date()
                let isDouble = lastTap.map { now.timeIntervalSince($0) <= Self.doubleTapWindow } ?? false
                lastTap = isDouble ? nil : now
                config.onTap(x, isDouble)
            case .scrub:
                config.onScrub(cancelled ? .cancelled : .ended, 0)
            case let .adjust(side):
                config.onAdjust(.ended, side, 0)
            case .hold:
                config.onHold(false)
            }
            intent = .undecided
        }
    }
}
