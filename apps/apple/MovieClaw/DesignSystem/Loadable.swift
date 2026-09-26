import SwiftUI

/// 页面数据的三态：加载中 / 失败 / 已加载。对应 Web 端「骨架屏 + 重试 + 内容」的统一写法。
enum Loadable<Value> {
    case loading
    case failed(String)
    case loaded(Value)

    var value: Value? {
        if case let .loaded(value) = self { return value }
        return nil
    }

    var isLoading: Bool {
        if case .loading = self { return true }
        return false
    }
}

/// 标准页面骨架：首次加载转圈、失败给出后端中文原因与「重试」、成功渲染内容。
///
/// 刷新时（已有数据）不会回到加载态，避免轮询/下拉刷新时整页闪烁。
/// ```swift
/// @State private var state: Loadable<API.LibraryView> = .loading
/// AsyncContent(state, retry: load) { library in … }
///     .task { await load() }
/// ```
struct AsyncContent<Value, Content: View>: View {
    let state: Loadable<Value>
    let retry: (() async -> Void)?
    /// 失败时的补充入口（例如 TMDB 不可达时给「网络设置」链接）
    var failureAction: (title: String, action: () -> Void)?
    @ViewBuilder let content: (Value) -> Content

    init(
        _ state: Loadable<Value>,
        retry: (() async -> Void)? = nil,
        failureAction: (title: String, action: () -> Void)? = nil,
        @ViewBuilder content: @escaping (Value) -> Content
    ) {
        self.state = state
        self.retry = retry
        self.failureAction = failureAction
        self.content = content
    }

    var body: some View {
        switch state {
        case .loading:
            ProgressView()
                .controlSize(.large)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .accessibilityIdentifier("loading")
        case let .failed(message):
            ErrorState(message: message, retry: retry, extra: failureAction)
        case let .loaded(value):
            content(value)
        }
    }
}

/// 失败态：图标 + 后端原因 + 重试
struct ErrorState: View {
    var title: String = "加载失败"
    let message: String
    var retry: (() async -> Void)?
    var extra: (title: String, action: () -> Void)?
    @State private var retrying = false

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: "exclamationmark.triangle")
        } description: {
            Text(message)
        } actions: {
            if let retry {
                Button {
                    retrying = true
                    Task { await retry(); retrying = false }
                } label: {
                    if retrying { ProgressView() } else { Text("重试") }
                }
                .buttonStyle(.glass)
                .disabled(retrying)
            }
            if let extra {
                Button(extra.title, action: extra.action)
            }
        }
        .accessibilityIdentifier("error-state")
    }
}

/// 空态：对应 Web ContentEmptyState（图标、标题、提示、行动按钮）
struct EmptyState: View {
    let systemImage: String
    let title: String
    var message: String?
    var actionTitle: String?
    var action: (() -> Void)?

    var body: some View {
        ContentUnavailableView {
            Label(title, systemImage: systemImage)
        } description: {
            if let message { Text(message) }
        } actions: {
            if let actionTitle, let action {
                Button(actionTitle, action: action).buttonStyle(.glass)
            }
        }
    }
}

extension Loadable {
    /// 执行加载：已有数据时保持原数据（静默刷新），失败时仅在没有数据的情况下进入失败态，
    /// 有数据时把错误交给 onRefreshError（通常弹 Toast）。
    static func load(
        into state: Binding<Loadable<Value>>,
        onRefreshError: ((Error) -> Void)? = nil,
        _ fetch: () async throws -> Value
    ) async {
        do {
            state.wrappedValue = .loaded(try await fetch())
        } catch is CancellationError {
            // 页面离开 / 任务被新请求替换，不改状态
        } catch {
            if state.wrappedValue.value == nil {
                state.wrappedValue = .failed(error.localizedDescription)
            } else {
                onRefreshError?(error)
            }
        }
    }
}
