import SwiftUI

/// 设置 → 网络（Web network-config-section.tsx + external-access-section.tsx）。
///
/// 交互模型与 Web 一致：**自动保存，立即生效**。代理方式 / 服务开关点按即落库，地址输入框提交或失焦落库；
/// 没有「保存」按钮，「测试」随时可点（先等在途保存落库，测的就是此刻看到的配置）。保存请求串行化，
/// 快速连点开关时按顺序落库，避免旧请求覆盖新配置。说明文字收进 ⓘ 气泡，页面只留字段本身。
///
/// 分组：代理（方式三选一 + 手动地址 / 环境变量探测结果）→ 走代理的服务（开关 + 逐项连通性测试）→
/// PT 站点 → 外部访问（外部访问地址失焦即存；对外端口要全量重启，单独二次确认，改完给出新地址）→
/// TMDB 镜像地址（高级，默认折叠）。
struct NetworkSettingsView: View {
    @Environment(\.api) private var api

    enum SaveState { case idle, saving, saved, error }
    enum TestState { case pending, done(API.NetworkTestResult) }
    enum Field: Hashable { case proxyURL, tmdbAPI, tmdbImage, externalURL, port }

    @State private var view: Loadable<API.NetworkConfigView> = .loading
    @State private var form = API.NetworkConfigPayload()
    @State private var saveState: SaveState = .idle
    @State private var saveError: String?
    @State private var proxyURLError: String?
    @State private var mirrorErrors: [String: String] = [:]
    @State private var tests: [String: TestState] = [:]
    @State private var advancedOpen = false
    @State private var saveChain: Task<Void, Never>?
    @State private var proxyURLDraft = ""
    @State private var tmdbAPIDraft = ""
    @State private var tmdbImageDraft = ""
    @FocusState private var focus: Field?

    private static let proxyPattern = #"^(http|https|socks5|socks5h)://"#

    var body: some View {
        AsyncContent(view, retry: reload) { current in
            List {
                proxySection(current)
                servicesSection(title: "走代理的服务", services: current.services.filter { !$0.id.hasPrefix("site:") }, current: current,
                                help: "按服务选择流量是否经过上面的代理；内网的下载器、媒体服务器永远直连，不在此列。\n\n经验默认：TMDB 与图片回源走代理（国内被墙）；豆瓣与 PT 站直连通常更快，且部分 PT 站风控在意出口 IP，按需开启。\n\n「测试」按当前配置发一次真实请求，熔断中的服务测通后立即恢复。")
                let sites = current.services.filter { $0.id.hasPrefix("site:") }
                if !sites.isEmpty {
                    servicesSection(title: "PT 站点", services: sites, current: current,
                                    help: "每个已配置的站点独立控制。国内 PT 站直连通常更快，且部分站点风控在意出口 IP——只给确实需要翻墙的站点开代理。")
                }
                ExternalAccessSections(focus: $focus)
                mirrorSection(current)
            }
            .scrollDismissesKeyboard(.interactively)
        }
        .appBackground()
        .task { await reload() }
        .onChange(of: focus) { old, _ in
            // 失焦即存（Web onBlur）
            switch old {
            case .proxyURL: commitProxyURL()
            case .tmdbAPI: commitMirror("tmdb_api_base_url", tmdbAPIDraft)
            case .tmdbImage: commitMirror("tmdb_image_base_url", tmdbImageDraft)
            default: break
            }
        }
    }

    private func reload() async {
        await Loadable.load(into: $view) { try await api.netShow() }
        if let v = view.value {
            form = .init(proxyMode: v.proxyMode, proxyUrl: v.proxyUrl, proxyServices: v.proxyServices,
                         tmdbApiBaseUrl: v.tmdbApiBaseUrl, tmdbImageBaseUrl: v.tmdbImageBaseUrl, doubanApiBaseUrl: v.doubanApiBaseUrl)
            proxyURLDraft = v.proxyUrl
            tmdbAPIDraft = v.tmdbApiBaseUrl
            tmdbImageDraft = v.tmdbImageBaseUrl
        }
    }

    // MARK: 落库

    /// 落库一份完整配置（串行）。手动模式地址为空 / 非法时只改表单不落库
    private func commit(_ next: API.NetworkConfigPayload) {
        form = next
        if next.proxyMode == "manual" {
            let url = (next.proxyUrl ?? "").trimmingCharacters(in: .whitespaces)
            if url.isEmpty {
                // 刚切到手动、地址还没填：等地址提交后再落库
                proxyURLError = nil
                return
            }
            if url.range(of: Self.proxyPattern, options: [.regularExpression, .caseInsensitive]) == nil {
                proxyURLError = "地址需以 http:// 、socks5:// 或 socks5h:// 开头"
                return
            }
        }
        proxyURLError = nil
        saveState = .saving
        saveError = nil
        let previous = saveChain
        saveChain = Task {
            await previous?.value
            do {
                let saved = try await api.netSet(body: next)
                view = .loaded(saved)
                // 后端会规范化镜像地址（补 /3、/t/p，去末尾斜杠），回填让用户看到实际生效的地址
                form.tmdbApiBaseUrl = saved.tmdbApiBaseUrl
                form.tmdbImageBaseUrl = saved.tmdbImageBaseUrl
                if focus != .tmdbAPI { tmdbAPIDraft = saved.tmdbApiBaseUrl }
                if focus != .tmdbImage { tmdbImageDraft = saved.tmdbImageBaseUrl }
                // 出口配置变了，旧的测试结论不再可信
                tests = [:]
                saveState = .saved
                try? await Task.sleep(for: .seconds(2))
                if saveState == .saved { saveState = .idle }
            } catch {
                saveState = .error
                saveError = error.localizedDescription
            }
        }
    }

    private func commitProxyURL() {
        let url = proxyURLDraft.trimmingCharacters(in: .whitespaces)
        guard url != (form.proxyUrl ?? "") || proxyURLError != nil else { return }
        var next = form
        next.proxyUrl = url
        commit(next)
    }

    private func commitMirror(_ field: String, _ raw: String) {
        let value = raw.trimmingCharacters(in: .whitespaces)
        let current = field == "tmdb_api_base_url" ? form.tmdbApiBaseUrl : form.tmdbImageBaseUrl
        guard value != (current ?? "") else {
            mirrorErrors[field] = nil
            return
        }
        if !value.isEmpty, value.range(of: #"^https?://"#, options: .regularExpression) == nil {
            mirrorErrors[field] = "需以 http(s):// 开头"
            return
        }
        mirrorErrors[field] = nil
        var next = form
        if field == "tmdb_api_base_url" { next.tmdbApiBaseUrl = value } else { next.tmdbImageBaseUrl = value }
        commit(next)
    }

    private func runTest(_ service: String) {
        tests[service] = .pending
        let pending = saveChain
        Task {
            // 先冲刷在途的保存，测试才反映用户此刻看到的配置
            await pending?.value
            do {
                tests[service] = .done(try await api.netTest(body: .init(service: service)))
            } catch {
                tests[service] = .done(.init(ok: false, latencyMs: nil, message: error.localizedDescription))
            }
        }
    }

    private func proxyActive(_ current: API.NetworkConfigView) -> Bool {
        switch form.proxyMode {
        case "manual": (form.proxyUrl ?? "").trimmingCharacters(in: .whitespaces).range(of: Self.proxyPattern, options: [.regularExpression, .caseInsensitive]) != nil
        case "env": !current.envProxyDetected.isEmpty
        default: false
        }
    }

    // MARK: 代理

    private func proxySection(_ current: API.NetworkConfigView) -> some View {
        Section {
            VStack(alignment: .leading, spacing: 10) {
                SettingsLabelWithHelp(
                    label: "代理方式",
                    help: "不使用：全部服务直连。\n\n环境变量：代理地址取自 HTTPS_PROXY / ALL_PROXY 等环境变量，Docker 部署用 -e HTTPS_PROXY=… 传入即可。\n\n手动：直接填写代理地址，支持 http 与 socks5。\n\n改动立即生效，无需重启。"
                )
                Picker("代理方式", selection: Binding(
                    get: { form.proxyMode ?? "off" },
                    set: { mode in
                        var next = form
                        next.proxyMode = mode
                        commit(next)
                    }
                )) {
                    Text("不使用").tag("off")
                    Text("环境变量").tag("env")
                    Text("手动").tag("manual")
                }
                .pickerStyle(.segmented)
                .accessibilityIdentifier("network-proxy-mode")
            }
            if form.proxyMode == "env" {
                HStack {
                    Text("环境变量探测").font(.subheadline).foregroundStyle(Theme.textMuted)
                    Spacer()
                    if current.envProxyDetected.isEmpty {
                        Text("未发现代理地址").font(.subheadline).foregroundStyle(Theme.warning)
                        SettingsHelpTip(text: "未检测到 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY。Docker 部署可通过 -e HTTPS_PROXY=… 传入；或改用「手动」直接填写。")
                    } else {
                        Text(current.envProxyDetected).font(.subheadline.monospaced())
                    }
                }
            }
            if form.proxyMode == "manual" {
                VStack(alignment: .leading, spacing: 6) {
                    SettingsLabelWithHelp(
                        label: "代理地址",
                        help: "NAS 上跑 Clash / sing-box 等工具时，填它的 HTTP 或 SOCKS5 入站地址。\n\n例：http://192.168.1.2:7890 或 socks5://192.168.1.2:7891。需要由代理端解析域名（对抗 DNS 污染）用 socks5h:// 前缀。"
                    )
                    TextField("socks5://192.168.1.2:7891", text: $proxyURLDraft)
                        .font(.subheadline.monospaced())
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused($focus, equals: .proxyURL)
                        .onSubmit { focus = nil }
                        .accessibilityIdentifier("network-proxy-url")
                    if proxyURLError != nil || proxyURLDraft.trimmingCharacters(in: .whitespaces).isEmpty {
                        Text(proxyURLError ?? "填写地址后自动保存生效")
                            .font(.caption).foregroundStyle(proxyURLError == nil ? Theme.textFaint : Theme.danger)
                    }
                }
            }
        } header: {
            HStack {
                Text("代理")
                Spacer()
                saveStatus.textCase(nil)
            }
        }
    }

    @ViewBuilder
    private var saveStatus: some View {
        switch saveState {
        case .idle: EmptyView()
        case .saving: Text("保存中…").font(.caption).foregroundStyle(Theme.textFaint)
        case .saved: Label("已保存，立即生效", systemImage: "checkmark").font(.caption).foregroundStyle(Theme.success)
        case .error: Text("保存失败：\(saveError ?? "")").font(.caption).foregroundStyle(Theme.danger).lineLimit(2)
        }
    }

    // MARK: 服务

    private func servicesSection(title: String, services: [API.EgressServiceOption], current: API.NetworkConfigView, help: String) -> some View {
        let active = proxyActive(current)
        let enabledServices = form.proxyServices ?? []
        return Section {
            ForEach(services, id: \.id) { service in
                HStack(spacing: 10) {
                    Text(service.label).font(.body.weight(.medium)).lineLimit(1)
                    SettingsHelpTip(text: service.description, label: "\(service.label)的说明")
                    Spacer(minLength: 4)
                    testResult(service.id)
                    Button("测试") { runTest(service.id) }
                        .buttonStyle(.glass).controlSize(.small)
                        .disabled({ if case .pending = tests[service.id] { return true }; return false }())
                        .accessibilityIdentifier("network-test-\(service.id)")
                    Toggle("\(service.label) 走代理", isOn: Binding(
                        get: { enabledServices.contains(service.id) },
                        set: { on in
                            var next = form
                            var list = next.proxyServices ?? []
                            if on { if !list.contains(service.id) { list.append(service.id) } } else { list.removeAll { $0 == service.id } }
                            next.proxyServices = list
                            commit(next)
                        }
                    ))
                    .labelsHidden()
                    .disabled(!active)
                    .accessibilityIdentifier("network-proxy-\(service.id)")
                }
            }
        } header: {
            HStack(spacing: 6) {
                Text(title)
                SettingsHelpTip(text: help).textCase(nil)
            }
        } footer: {
            if !active, title == "走代理的服务" {
                Text("当前无可用代理，开关已禁用（测试仍可用，测的是直连/镜像的连通性）")
            }
        }
    }

    @ViewBuilder
    private func testResult(_ service: String) -> some View {
        switch tests[service] {
        case .pending:
            Text("测试中…").font(.caption).foregroundStyle(Theme.textFaint)
        case let .done(result):
            HStack(spacing: 4) {
                SettingsStatusDot(color: result.ok ? Theme.success : Theme.danger, size: 6)
                Text(result.ok ? (result.latencyMs.map { "连通 · \($0) ms" } ?? "连通") : "不通")
                    .font(.caption).foregroundStyle(result.ok ? Theme.success : Theme.danger)
                SettingsHelpTip(text: result.message, label: "测试结果详情")
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("network-test-result-\(service)")
        case nil:
            EmptyView()
        }
    }

    // MARK: 镜像

    private func mirrorSection(_ current: API.NetworkConfigView) -> some View {
        Section {
            DisclosureGroup(isExpanded: $advancedOpen) {
                mirrorField(field: "tmdb_api_base_url", label: "接口地址",
                            help: "替换 api.themoviedb.org 的官方接口地址（发现页/搜索/订阅建档用）。只填到域名即可，会自动补上官方的 /3 后缀；自己写了路径则按你写的用。留空使用默认值。",
                            text: $tmdbAPIDraft, focusField: .tmdbAPI, placeholder: current.mirrorDefaults["tmdb_api_base_url"] ?? "")
                mirrorField(field: "tmdb_image_base_url", label: "图床地址",
                            help: "替换 image.tmdb.org 的图床地址（海报/背景图回源用）。只填到域名即可，会自动补上官方的 /t/p 后缀；自己写了路径则按你写的用。留空使用默认值。",
                            text: $tmdbImageDraft, focusField: .tmdbImage, placeholder: current.mirrorDefaults["tmdb_image_base_url"] ?? "")
            } label: {
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("TMDB 镜像地址").font(.body.weight(.medium))
                        Text("不走代理的替代方案").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    SettingsHelpTip(text: "解决「TMDB 不可达」有两条独立的路：代理让流量绕行（访问地址不变）；镜像把官方地址换成一个可直连的反代地址（流量不变、地址变了）。\n\n有代理就不用配镜像，二选一即可。若两者都设置，请求会经代理去访问镜像地址。\n\n镜像可以是自建反代（nginx / Cloudflare Workers）或公共镜像；注意公共镜像会经手你的 API Key，稳定性与隐私自行权衡。")
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("network-mirror")
        }
    }

    private func mirrorField(field: String, label: String, help: String, text: Binding<String>, focusField: Field, placeholder: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            SettingsLabelWithHelp(label: label, help: help)
            TextField(placeholder, text: text)
                .font(.subheadline.monospaced())
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .focused($focus, equals: focusField)
                .onSubmit { focus = nil }
            if let error = mirrorErrors[field] {
                Text(error).font(.caption).foregroundStyle(Theme.danger)
            }
        }
        .padding(.vertical, 4)
    }
}

// MARK: - 外部访问

/// 外部访问地址 / 对外端口（Web external-access-section.tsx，走 `/app/config` 与 `/app/port`）。
///
/// 端口是本组唯一不走「失焦即存」的字段：改它要全量重启，且改完当前地址多半打不开了——
/// 所以要二次确认，并在确认里直白写出「bridge 网络下 compose 的 ports 映射也要同步改」这条代码兜不住的后果。
private struct ExternalAccessSections: View {
    var focus: FocusState<NetworkSettingsView.Field?>.Binding
    @Environment(\.api) private var api

    enum PortPhase { case idle, confirming, switched }

    @State private var config: API.AppConfigView?
    @State private var failed = false
    @State private var saveState: NetworkSettingsView.SaveState = .idle
    @State private var saveError: String?
    @State private var urlDraft = ""
    @State private var urlError: String?
    @State private var portDraft = ""
    @State private var portError: String?
    @State private var portPhase: PortPhase = .idle
    /// 待确认 / 已提交的目标端口；nil = 恢复默认
    @State private var portTarget: Int?

    /// App 当前连接的地址（相当于 Web 的「浏览器地址栏」）
    private var origin: String {
        var value = api.server.origin.absoluteString
        while value.hasSuffix("/") { value.removeLast() }
        return value
    }

    /// App 此刻实际访问的端口（省略时按协议补 80/443）；与应用监听端口不等 = 中间有端口映射或反代
    private var seenPort: Int {
        let url = api.server.origin
        return url.port ?? (url.scheme == "https" ? 443 : 80)
    }

    private func portURL(_ port: Int) -> String {
        let url = api.server.origin
        return "\(url.scheme ?? "http")://\(url.host() ?? ""):\(port)"
    }

    var body: some View {
        Group {
            if failed {
                Section("外部访问") {
                    HStack {
                        Text("外部访问设置加载失败").foregroundStyle(Theme.textMuted)
                        Spacer()
                        Button("重试") { Task { await load() } }.buttonStyle(.glass)
                    }
                }
            } else if let config {
                if portPhase == .switched {
                    switchedSection(config)
                } else {
                    formSection(config)
                }
            } else {
                Section("外部访问") { SettingsLoadingRow(text: "正在加载外部访问设置…") }
            }
        }
        .task { await load() }
        .onChange(of: focus.wrappedValue) { old, _ in
            if old == .externalURL { commitURL(urlDraft) }
        }
    }

    private func load() async {
        failed = false
        do {
            let value = try await api.appShow()
            config = value
            urlDraft = value.externalUrl
            portDraft = String(value.webPort)
        } catch {
            failed = true
        }
    }

    private func commitURL(_ raw: String) {
        guard let config else { return }
        let url = raw.trimmingCharacters(in: .whitespaces)
        guard url != config.externalUrl else {
            urlError = nil
            return
        }
        if !url.isEmpty, url.range(of: #"^https?://.+"#, options: .regularExpression) == nil {
            urlError = "需以 http:// 或 https:// 开头的完整地址"
            return
        }
        urlError = nil
        save(url)
    }

    private func save(_ url: String) {
        saveState = .saving
        saveError = nil
        Task {
            do {
                let next = try await api.appSet(body: .init(externalUrl: url))
                config = next
                urlDraft = next.externalUrl
                saveState = .saved
                try? await Task.sleep(for: .seconds(2))
                if saveState == .saved { saveState = .idle }
            } catch {
                saveState = .error
                saveError = error.localizedDescription
            }
        }
    }

    /// 校验端口草稿并进入二次确认（真正提交在 savePort）
    private func reviewPort(_ config: API.AppConfigView) {
        let text = portDraft.trimmingCharacters(in: .whitespaces)
        portError = nil
        guard !text.isEmpty else {
            portError = "请输入端口，或点「恢复默认」清除设置"
            return
        }
        guard text.allSatisfy(\.isNumber), let port = Int(text), (1 ... 65535).contains(port) else {
            portError = "端口需是 1~65535 的整数"
            return
        }
        guard port != config.webPort else {
            portError = "与当前端口相同"
            return
        }
        portTarget = port
        portPhase = .confirming
    }

    /// 成功后不轮询等恢复——应用会在新端口上起来，本地址多半已经不通，直接给出新地址
    private func savePort(_ config: API.AppConfigView) async {
        do {
            let next = try await api.appPortSet(body: .init(port: portTarget ?? 0))
            if next.webPort == config.webPort {
                // 生效端口其实没变（清除设置后回落值恰好等于当前端口），后端不会重启
                self.config = next
                portPhase = .idle
                return
            }
            portPhase = .switched
        } catch {
            portPhase = .idle
            portError = error.localizedDescription
        }
    }

    private func formSection(_ config: API.AppConfigView) -> some View {
        let behindMapping = seenPort != config.webPort
        let target = portTarget ?? config.webPortDefault
        return Section {
            VStack(alignment: .leading, spacing: 6) {
                SettingsLabelWithHelp(
                    label: "外部访问地址",
                    help: "从网络上能访问到本应用的完整地址，保存即生效。通常就是你浏览器地址栏正在使用的地址（输入框的提示即 App 当前连接的地址，照填即可）。\n\n若经反向代理 / 域名访问，请填代理后的对外地址，如 https://movie.example.com。\n\n用于生成通知里的跳转链接、对外回调地址等需要绝对 URL 的场景。"
                )
                HStack(spacing: 8) {
                    TextField(origin, text: $urlDraft)
                        .font(.subheadline.monospaced())
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused(focus, equals: .externalURL)
                        .onSubmit { focus.wrappedValue = nil }
                        .accessibilityIdentifier("network-external-url")
                    // 未设置且没在输入时，框内给「使用」一键填入当前地址
                    if config.externalUrl.isEmpty, urlDraft.trimmingCharacters(in: .whitespaces).isEmpty {
                        Button("使用") { save(origin) }
                            .buttonStyle(.glass).controlSize(.mini)
                            .disabled(saveState == .saving)
                    }
                }
                if let urlError {
                    Text(urlError).font(.caption).foregroundStyle(Theme.danger)
                } else if config.externalUrl.isEmpty {
                    Text("点「使用」采用当前地址，通知与 AI 回复才能带上页面链接").font(.caption).foregroundStyle(Theme.textFaint)
                }
            }
            VStack(alignment: .leading, spacing: 6) {
                SettingsLabelWithHelp(
                    label: "对外端口",
                    help: "本应用对外监听的端口，默认 \(config.webPortDefault)。保存后应用会全量重启，之后必须用新端口访问。\n\n用 Docker 端口映射（bridge 网络）部署时，改这里等于改容器侧端口，compose 的 ports 也要同步改，否则改完就访问不到——这种部署下只改宿主侧映射（如 8096:3000）更省事，不必动这个设置。\n\n真正需要它的是 host 网络 / 裸机直连：容器内端口就是宿主端口，撞了端口只能从这里避让。\n\n也可以用环境变量 MOVIECLAW_WEB_PORT 在部署时定死；这里的设置优先级更高。"
                )
                HStack(spacing: 8) {
                    TextField("", text: $portDraft)
                        .font(.subheadline.monospaced())
                        .keyboardType(.numberPad)
                        .focused(focus, equals: .port)
                        .disabled(!config.webPortConfigurable)
                        .accessibilityIdentifier("network-port")
                    if config.webPortConfigurable, !portDraft.trimmingCharacters(in: .whitespaces).isEmpty,
                       portDraft.trimmingCharacters(in: .whitespaces) != String(config.webPort) {
                        Button("修改") {
                            focus.wrappedValue = nil
                            reviewPort(config)
                        }
                        .buttonStyle(.glass).controlSize(.small)
                        .accessibilityIdentifier("network-port-review")
                    }
                }
                Group {
                    if let portError {
                        Text(portError).foregroundStyle(Theme.danger)
                    } else if !config.webPortConfigurable {
                        Text("当前部署形态由外部启动前端进程，端口请在启动命令或反向代理处调整")
                    } else if config.webPortSource == "setting" {
                        HStack(spacing: 8) {
                            Text("应用内设置")
                            Button("恢复默认（\(config.webPortDefault)）") {
                                portTarget = nil
                                portPhase = .confirming
                            }
                            .buttonStyle(.plain).underline()
                        }
                    } else if config.webPortSource == "env" {
                        Text("来自环境变量 MOVIECLAW_WEB_PORT，在此修改会覆盖它")
                    } else {
                        Text("默认端口，改动后需全量重启生效")
                    }
                }
                .font(.caption).foregroundStyle(Theme.textFaint)
                if let rejected = config.webPortRejected {
                    Text("上次设置的端口 \(rejected) 无法绑定（多半是被占用），已自动废弃并回落到 \(config.webPort)")
                        .font(.caption).foregroundStyle(Theme.warning)
                }
            }
            if portPhase == .confirming {
                VStack(alignment: .leading, spacing: 8) {
                    Text((portTarget == nil
                          ? "确认清除端口设置、恢复默认 \(config.webPortDefault)？"
                          : "确认把对外端口从 \(config.webPort) 改为 \(target)？")
                         + "应用会全量重启，之后要用新地址 \(portURL(target)) 访问。")
                        .font(.subheadline)
                    Text(behindMapping
                         ? "检测到你正通过端口 \(seenPort) 访问，而应用监听的是 \(config.webPort)——中间存在端口映射或反向代理。只改这里会打断那条链路，你必须同时把映射/反代的目标端口改成 \(target)（Docker 就是 compose 里 ports 的右侧，改完重建容器）。如果你只是想换访问端口，改映射的左侧更省事，不必动这个设置。"
                         : "若用 Docker 端口映射（bridge）部署，请同时把 compose 里 ports 的容器侧端口改成 \(target) 并重建容器——否则重启后将无法访问，届时只能改 compose 恢复。host 网络或裸机直连则无需任何额外改动。")
                        .font(.caption).foregroundStyle(behindMapping ? Theme.danger : Theme.textMuted)
                    HStack(spacing: 10) {
                        Button("确认修改") { Task { await savePort(config) } }
                            .settingsProminentButton().controlSize(.small)
                            .accessibilityIdentifier("network-port-confirm")
                        Button("取消") { portPhase = .idle }
                            .buttonStyle(.glass).controlSize(.small)
                            .accessibilityIdentifier("network-port-cancel")
                    }
                }
                .padding(12)
                .background(Theme.warning.opacity(0.1), in: .rect(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Theme.warning.opacity(0.25)))
            }
        } header: {
            HStack {
                Text("外部访问")
                Spacer()
                Group {
                    switch saveState {
                    case .idle: EmptyView()
                    case .saving: Text("保存中…").foregroundStyle(Theme.textFaint)
                    case .saved: Label("已保存", systemImage: "checkmark").foregroundStyle(Theme.success)
                    case .error: Text("保存失败：\(saveError ?? "")").foregroundStyle(Theme.danger).lineLimit(2)
                    }
                }
                .font(.caption).textCase(nil)
            }
        }
    }

    /// 端口已改：应用正在新端口上重启，本地址已失效——不轮询，直接给新地址
    private func switchedSection(_ config: API.AppConfigView) -> some View {
        let target = portTarget ?? config.webPortDefault
        return Section("外部访问") {
            VStack(spacing: 10) {
                Text("对外端口已改为 \(target)，应用正在重启…").font(.body.weight(.medium))
                Text("重启后当前地址不再可用，请改用新端口访问（通常几十秒内起来）。").font(.subheadline).foregroundStyle(Theme.textMuted)
                Text(portURL(target)).font(.subheadline.monospaced()).textSelection(.enabled)
                Text("用 Docker 端口映射（bridge 网络）部署时，还要把 compose 里 ports 的容器侧端口改成 \(target) 并重建容器，上面的地址才会通；host 网络或裸机直连则直接换端口访问即可。App 这边需退出登录，在登录页点「更换服务器」改填新地址。")
                    .font(.caption).foregroundStyle(Theme.textFaint)
            }
            .multilineTextAlignment(.center)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 12)
        }
    }
}
