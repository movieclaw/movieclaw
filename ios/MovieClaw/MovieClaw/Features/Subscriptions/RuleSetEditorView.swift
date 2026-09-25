import SwiftUI

/// 规则组编辑器（对应 Web `RuleSetEditorDialog`，components/rule-sets-panel.tsx）。
///
/// 可复用组件：订阅弹层 / 洗一轮版里的「+ 新建规则组」、之后「设置 → 订阅规则」的新建 / 编辑 / 复制
/// 都直接弹这个 sheet：
/// ```swift
/// .sheet(isPresented: $creating) { RuleSetEditorSheet(ruleSet: nil) { saved in … } }
/// .sheet(item: $editing) { rule in RuleSetEditorSheet(ruleSet: rule) { _ in reload() } }
/// RuleSetEditorSheet(ruleSet: nil, template: (name: "\(rule.name) 副本", spec: rule.typedSpec)) { … } // 复制
/// ```
///
/// 设计原则同 Web：
/// - **表单化，不手写 JSON**：spec 的每个维度铺成芯片 / 开关 / 输入框；
/// - **列表顺序即偏好**：分辨率、片源、洗版维度按点击顺序入列，芯片上标序号；
/// - **渐进披露**：「适用范围」「画质与来源」「下载与限制」默认折叠，折叠头直接显示当前摘要；
/// - **所见即所存**：底部回执与保存共用同一个 `draft`，看到的摘要就是将要存下去的东西；
/// - **矛盾在源头消除**：平台 / HDR 三态循环不可能同时进白黑名单；收窄片源会清掉不在范围内的洗版终点。
struct RuleSetEditorSheet: View {
    /// nil = 新建
    let ruleSet: API.RuleSetView?
    /// 新建时的预填（复制场景）；编辑时忽略
    var template: (name: String, spec: RuleSetSpec)?
    /// 保存成功，参数是后端返回的最新规则组（快捷新建场景据此选中它）
    let onSaved: (API.RuleSetView) -> Void

    @Environment(\.api) private var api
    @Environment(\.dismiss) private var dismiss

    @State private var loaded = false
    @State private var original = RuleSetSpec()
    @State private var name = ""
    @State private var scope = RuleSetScope()
    @State private var routingOptions: LibraryRoutingOptions?
    @State private var resolutions: [String] = []
    @State private var mediaSources: [String] = []
    @State private var codecFamilies: Set<String> = []
    @State private var platformsAllow: [String] = []
    @State private var platformsBlock: [String] = []
    @State private var hdrLevels: [String] = []
    @State private var hdrBlock: [String] = []
    @State private var subLangs: [String] = []
    @State private var audioLangs: [String] = []
    @State private var freeOnly = false
    @State private var excludeHr = false
    @State private var hrStrict = false
    @State private var minSeeders = ""
    @State private var sizeMin = ""
    @State private var sizeMax = ""
    @State private var groupsAllow = ""
    @State private var groupsBlock = ""
    /// "" = 不洗版
    @State private var upgradeSource = ""
    @State private var upgradeKeepOld = false
    /// "" = 跟随分辨率偏好首位
    @State private var cutoffResolution = ""
    @State private var upgradeLadder = RuleSetVocabulary.defaultLadder
    @State private var ladderOpen = false
    @State private var ladderPreviewOpen = false
    @State private var openSections: Set<String> = []
    @State private var busy = false
    @State private var error: String?

    // MARK: 派生

    /// 编码族之外的原有值（经 API 写入的自定义编码）：编辑时不丢
    private var codecExtras: [String] {
        original.videoCodecs.filter { value in !RuleSetVocabulary.codecFamilies.contains { $0.values.contains(value) } }
    }

    /// 常驻选项之外的既有平台值：编辑时不丢
    private var platformExtras: (allow: [String], block: [String]) {
        (original.platforms.filter { !RuleSetVocabulary.platformOptions.contains($0) },
         original.platformsBlock.filter { !RuleSetVocabulary.platformOptions.contains($0) })
    }

    private func ladderUnconfigured(_ dim: String) -> Bool {
        (dim == "hdr" && hdrLevels.isEmpty)
            || (dim == "video_codec" && codecFamilies.isEmpty && codecExtras.isEmpty)
            || (dim == "platform" && platformsAllow.isEmpty && platformExtras.allow.isEmpty)
    }

    /// 表单状态 → spec 的唯一构造点（保存与底部回执共用），校验错误随返回值给出
    private var draft: (spec: RuleSetSpec, error: String?) {
        func parseInt(_ text: String) -> Int? {
            guard let n = Int(text.trimmingCharacters(in: .whitespaces)), n >= 0 else { return nil }
            return n
        }
        func parseGroups(_ text: String) -> [String] {
            text.components(separatedBy: CharacterSet(charactersIn: ",，、").union(.whitespacesAndNewlines)).filter { !$0.isEmpty }
        }
        var next = RuleSetSpec()
        next.resolutions = resolutions
        next.mediaSources = mediaSources
        next.videoCodecs = RuleSetVocabulary.codecFamilies.filter { codecFamilies.contains($0.label) }.flatMap(\.values) + codecExtras
        if !upgradeSource.isEmpty, upgradeLadder != RuleSetVocabulary.defaultLadder { next.upgradeLadder = upgradeLadder }
        next.platforms = platformsAllow + platformExtras.allow
        next.platformsBlock = platformsBlock + platformExtras.block
        next.hdrLevels = hdrLevels
        next.hdrBlock = hdrBlock
        next.subtitleLanguages = subLangs
        next.audioLanguages = audioLangs
        next.freeOnly = freeOnly
        next.excludeHr = excludeHr
        next.hrUnknownPolicy = excludeHr && hrStrict ? "strict" : nil
        if let seeders = parseInt(minSeeders), seeders > 0 { next.minSeeders = seeders }
        let min = parseInt(sizeMin), max = parseInt(sizeMax)
        if let min, min > 0 { next.sizeMinMb = min }
        if let max, max > 0 { next.sizeMaxMb = max }
        if let min, let max, min > 0, max > 0, min > max { return (next, "体积下限不能大于上限") }
        next.releaseGroupsAllow = parseGroups(groupsAllow)
        next.releaseGroupsBlock = parseGroups(groupsBlock)
        if !upgradeSource.isEmpty {
            if !mediaSources.isEmpty, !mediaSources.contains(upgradeSource) {
                return (next, "洗版目标片源必须在允许的片源范围内")
            }
            next.upgradeSource = upgradeSource
            if !cutoffResolution.isEmpty, cutoffResolution != (resolutions.first ?? "") {
                if !resolutions.isEmpty, !resolutions.contains(cutoffResolution) {
                    return (next, "洗版目标分辨率必须在允许的分辨率范围内")
                }
                next.cutoffResolution = cutoffResolution
            }
            next.upgradeKeepOld = upgradeKeepOld
        }
        next.sites = original.sites
        return (next, nil)
    }

    private var validScope: RuleSetScope {
        var result = scope
        if let routingOptions {
            let allowed = Set(routingOptions.genres(for: scope.kind).map(\.id))
            result.genres = scope.genres.filter(allowed.contains)
        }
        return result
    }

    private var qualitySummary: String {
        let parts = [
            codecFamilies.isEmpty ? "" : RuleSetVocabulary.codecFamilies.map(\.label).filter(codecFamilies.contains).joined(separator: "/"),
            platformsAllow.isEmpty ? "" : platformsAllow.map(RuleSetVocabulary.platformLabel).joined(separator: "/"),
            platformsBlock.isEmpty ? "" : "排除 " + platformsBlock.map(RuleSetVocabulary.platformLabel).joined(separator: "/"),
            hdrLevels.isEmpty ? "" : RuleSetText.hdrChipText(hdrLevels),
            hdrBlock.isEmpty ? "" : "排除 " + hdrBlock.joined(separator: "/"),
            groupsAllow.trimmingCharacters(in: .whitespaces).isEmpty ? "" : "组 " + groupsAllow.trimmingCharacters(in: .whitespaces),
            groupsBlock.trimmingCharacters(in: .whitespaces).isEmpty ? "" : "排除组 " + groupsBlock.trimmingCharacters(in: .whitespaces),
        ].filter { !$0.isEmpty }
        return parts.isEmpty ? "未设置" : parts.joined(separator: " · ")
    }

    private var limitSummary: String {
        func labels(_ options: [(value: String, label: String)], _ values: [String]) -> String {
            values.map { v in options.first { $0.value == v }?.label ?? v }.joined(separator: "/")
        }
        let min = sizeMin.trimmingCharacters(in: .whitespaces), max = sizeMax.trimmingCharacters(in: .whitespaces)
        let parts = [
            subLangs.isEmpty ? "" : "字幕 " + labels(RuleSetVocabulary.subtitleLanguages, subLangs),
            audioLangs.isEmpty ? "" : "音轨 " + labels(RuleSetVocabulary.audioLanguages, audioLangs),
            freeOnly ? "仅免费" : "",
            excludeHr ? "排除 H&R" : "",
            minSeeders.trimmingCharacters(in: .whitespaces).isEmpty ? "" : "做种 ≥ \(minSeeders.trimmingCharacters(in: .whitespaces))",
            min.isEmpty && max.isEmpty ? "" : "体积 \(min.isEmpty ? "0" : min)–\(max.isEmpty ? "∞" : max)MB",
        ].filter { !$0.isEmpty }
        return parts.isEmpty ? "未设置" : parts.joined(separator: " · ")
    }

    // MARK: 视图

    var body: some View {
        let draft = draft
        let ladderPreview = UpgradeLadderPreview(draft.spec)
        let draftChips = RuleSetText.summary(draft.spec, withoutUpgrade: ladderPreview != nil)
        SubsSheetScaffold(
            title: ruleSet == nil ? "新建规则组" : "编辑规则组",
            subtitle: "所有条件都可以留空 = 不限该维度；条件之间是「且」的关系。"
        ) {
            if let ruleSet, ruleSet.referenceCount > 0 {
                SubsNotice(text: "此组正被 \(ruleSet.referenceCount) 个订阅使用，保存后对它们之后的资源评估立即生效（已下载的内容不受影响）。", tone: .warn)
            }
            field("名称") {
                TextField("如：4K 免费、追剧省流", text: $name)
                    .textFieldStyle(.plain)
                    .padding(.horizontal, 14).padding(.vertical, 11)
                    .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 12))
                    .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
                    .accessibilityIdentifier("ruleset-name")
            }

            collapsible("适用范围", key: "scope", summary: validScope.summary(routingOptions) ?? "未设置（只能手动选用，或作为默认组兜底）") {
                Text("订阅的作品符合这里的条件时，自动选用本规则组；多个组都符合时条件更多的优先，都不符合时用默认组。只选「电影」或「剧集」即可让本组成为该类型的默认选择。")
                    .font(.caption).foregroundStyle(Theme.textFaint)
                field("作品类型") {
                    chips {
                        ForEach([(String?.none, "不限"), ("movie", "电影"), ("tv", "剧集")], id: \.1) { value, label in
                            SubsToggleChip(label: label, active: scope.kind == value) { scope.kind = value }
                        }
                    }
                }
                scopeEditor
            }

            field("分辨率", hint: "点击依次选择，先选的优先（选中顺序 = 下载偏好）；不选 = 不限。限定分辨率后，无法从种子名识别出分辨率的资源也会被排除，可用「手动选种」兜底") {
                chips {
                    ForEach(RuleSetVocabulary.resolutions, id: \.self) { option in
                        let index = resolutions.firstIndex(of: option)
                        SubsToggleChip(label: option, active: index != nil, order: index != nil && resolutions.count > 1 ? (index ?? 0) + 1 : nil) {
                            toggle(&resolutions, option)
                        }
                    }
                }
            }

            field("片源", hint: "点击依次选择，先选的优先（选中顺序 = 下载偏好）；不选 = 不限。Rip 类 = WEBRip/BDRip，电视录制类 = HDTV/DVD。限定片源后，无法从种子名识别出片源的资源也会被排除，可用「手动选种」兜底") {
                chips {
                    ForEach(RuleSetVocabulary.mediaSources, id: \.value) { option in
                        let index = mediaSources.firstIndex(of: option.value)
                        SubsToggleChip(label: option.label, active: index != nil, order: index != nil && mediaSources.count > 1 ? (index ?? 0) + 1 : nil) {
                            toggle(&mediaSources, option.value)
                            // 收窄白名单时洗版终点被排除在外，直接清掉终点
                            if !upgradeSource.isEmpty, !mediaSources.isEmpty, !mediaSources.contains(upgradeSource) { upgradeSource = "" }
                        }
                    }
                }
            }

            collapsible("画质与来源", key: "quality", summary: qualitySummary) {
                field("视频编码", hint: "按家族选择，等价写法（如 x265 / HEVC）一并计入；不选 = 不限") {
                    chips {
                        ForEach(RuleSetVocabulary.codecFamilies, id: \.label) { family in
                            SubsToggleChip(label: family.label, active: codecFamilies.contains(family.label)) {
                                if codecFamilies.contains(family.label) { codecFamilies.remove(family.label) } else { codecFamilies.insert(family.label) }
                            }
                        }
                    }
                    if !codecExtras.isEmpty {
                        Text("另有自定义值：\(codecExtras.joined(separator: "、"))（保留）").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                field("流媒体平台", hint: "点一下 = 只要，再点 = 排除，第三下取消。设了「只要」之后，识别不出平台的资源会被排除（与分辨率/编码口径一致）") {
                    chips {
                        ForEach(RuleSetVocabulary.platformOptions, id: \.self) { id in
                            SubsTriChip(label: RuleSetVocabulary.platformLabel(id), state: triState(id, allow: platformsAllow, block: platformsBlock)) {
                                cycle(id, allow: &platformsAllow, block: &platformsBlock)
                            }
                        }
                    }
                    let extras = platformExtras.allow + platformExtras.block
                    if !extras.isEmpty {
                        Text("另有自定义值：\(extras.map(RuleSetVocabulary.platformLabel).joined(separator: "、"))（保留）").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
                field("HDR", hint: "点一下 = 只要，再点 = 排除，第三下取消。\"SDR\" 指资源没标注任何 HDR 格式；都不选 = 不限") {
                    chips {
                        ForEach(RuleSetVocabulary.hdrOptions, id: \.self) { value in
                            SubsTriChip(label: value, state: triState(value, allow: hdrLevels, block: hdrBlock)) {
                                cycle(value, allow: &hdrLevels, block: &hdrBlock)
                            }
                        }
                    }
                }
                field("制作组白名单", hint: "只接受这些制作组的资源，逗号或空格分隔（如 FRDS, WiKi）；留空 = 不限") {
                    textInput("留空不限", text: $groupsAllow)
                }
                field("制作组黑名单", hint: "这些制作组的资源一律不要") {
                    textInput("留空不启用", text: $groupsBlock)
                }
            }

            collapsible("下载与限制", key: "limits", summary: limitSummary) {
                field("字幕语言", hint: "任一命中即通过；按种子标题声明判断——未声明字幕的资源会被排除，不选 = 不限") {
                    chips {
                        ForEach(RuleSetVocabulary.subtitleLanguages, id: \.value) { option in
                            SubsToggleChip(label: option.label, active: subLangs.contains(option.value)) { toggle(&subLangs, option.value) }
                        }
                    }
                }
                field("音轨语言", hint: "任一命中即通过；按种子标题声明判断——未声明音轨的资源会被排除，不选 = 不限") {
                    chips {
                        ForEach(RuleSetVocabulary.audioLanguages, id: \.value) { option in
                            SubsToggleChip(label: option.label, active: audioLangs.contains(option.value)) { toggle(&audioLangs, option.value) }
                        }
                    }
                }
                SubsToggleRow(title: "只要免费资源", hint: "促销状态未知的按非免费处理", isOn: $freeOnly)
                SubsToggleRow(title: "排除 H&R 考核种子", hint: "有做种考核要求的资源不下载", isOn: $excludeHr)
                if excludeHr {
                    SubsToggleRow(title: "站点未提供 H&R 信息时，保守视作有考核而排除", isOn: $hrStrict)
                }
                field("做种数下限") { numberInput(text: $minSeeders) }
                field("单集体积下限 (MB)") { numberInput(text: $sizeMin) }
                field("单集体积上限 (MB)") { numberInput(text: $sizeMax) }
                Text("体积按「每集均摊」评估：整季包用总体积 ÷ 集数比较，整季合集不会被单集上限误杀。")
                    .font(.caption).foregroundStyle(Theme.textFaint)
            }

            upgradeField

            VStack(alignment: .leading, spacing: 6) {
                Text(ladderPreview != nil ? "先这样筛掉不要的" : "这条规则会这样筛选").font(.caption).foregroundStyle(Theme.textFaint)
                Text(draftChips.isEmpty ? "不限任何条件——身份对得上的资源都接受" : draftChips.joined(separator: " · "))
                    .font(.subheadline).foregroundStyle(Theme.textMuted)
                    .accessibilityIdentifier("ruleset-draft-summary")
                if let ladderPreview { ladderPreviewView(ladderPreview) }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 14))

            if let error { SubsNotice(text: error, tone: .error) }
        } footer: {
            SubsPrimaryButton(
                title: busy ? "正在保存…" : "保存",
                busy: busy,
                enabled: !name.trimmingCharacters(in: .whitespaces).isEmpty,
                identifier: "ruleset-save"
            ) { Task { await submit() } }
        }
        .interactiveDismissDisabled(busy)
        .accessibilityIdentifier("ruleset-editor")
        .task {
            guard !loaded else { return }
            loaded = true
            populate()
            routingOptions = try? await api.routingOptions()
        }
    }

    // MARK: 分段

    @ViewBuilder
    private var scopeEditor: some View {
        if let options = routingOptions {
            field("区域（勾选任一即匹配）") {
                let known = Set(options.countryNames.keys)
                let countries = options.sortedCountries + scope.regions.filter { !known.contains($0) }.map { ($0, $0) }
                chips {
                    ForEach(countries, id: \.code) { country in
                        SubsToggleChip(label: country.name, active: scope.regions.contains(country.code)) { toggle(&scope.regions, country.code) }
                    }
                }
                chips {
                    Text("快捷组合").font(.caption).foregroundStyle(Theme.textFaint)
                    let allCodes = Array(options.countryNames.keys)
                    let allOn = allCodes.allSatisfy(scope.regions.contains)
                    SubsToggleChip(label: allOn ? "清空" : "全选", active: false) {
                        scope.regions = allOn ? [] : Array(Set(scope.regions + allCodes)).sorted()
                    }
                    ForEach(options.regionPresets) { preset in
                        let active = preset.countries.allSatisfy(scope.regions.contains)
                        SubsToggleChip(label: preset.label, active: active) {
                            if active {
                                scope.regions.removeAll { preset.countries.contains($0) }
                            } else {
                                for code in preset.countries where !scope.regions.contains(code) { scope.regions.append(code) }
                            }
                        }
                    }
                }
            }
            field("类型（勾选任一即匹配）") {
                let genres = options.genres(for: scope.kind)
                let allOn = genres.allSatisfy { scope.genres.contains($0.id) }
                chips {
                    SubsToggleChip(label: allOn ? "清空" : "全选", active: false) {
                        scope.genres = allOn ? [] : Array(Set(scope.genres + genres.map(\.id)))
                    }
                    ForEach(genres) { genre in
                        SubsToggleChip(label: genre.label, active: scope.genres.contains(genre.id)) {
                            if let i = scope.genres.firstIndex(of: genre.id) { scope.genres.remove(at: i) } else { scope.genres.append(genre.id) }
                        }
                    }
                }
            }
        } else {
            Text("正在加载可选项…").font(.subheadline).foregroundStyle(Theme.textFaint)
        }
    }

    private var upgradeField: some View {
        field("洗版", hint: "收齐后继续追更高版本，直到达到目标档位为止。新版本入库后旧版本进回收站保留 7 天，做种中的任务不受影响；不开启 = 下到即止") {
            chips {
                SubsToggleChip(label: "不洗版", active: upgradeSource.isEmpty) { upgradeSource = "" }
                ForEach(RuleSetVocabulary.upgradeOptions.filter { mediaSources.isEmpty || mediaSources.contains($0.value) }, id: \.value) { option in
                    SubsToggleChip(label: option.label, active: upgradeSource == option.value) { upgradeSource = option.value }
                }
            }
            if !upgradeSource.isEmpty {
                VStack(alignment: .leading, spacing: 10) {
                    Text("目标分辨率").font(.subheadline).foregroundStyle(Theme.textMuted)
                    chips {
                        let effective = cutoffResolution.isEmpty ? (resolutions.first ?? "1080p") : cutoffResolution
                        ForEach(resolutions.isEmpty ? RuleSetVocabulary.resolutions : resolutions, id: \.self) { option in
                            SubsToggleChip(label: option, active: effective == option) {
                                cutoffResolution = option == (resolutions.first ?? "") ? "" : option
                            }
                        }
                    }
                    Text(resolutions.isEmpty ? "未限定分辨率时缺省洗到 1080p（避免意外进入 4K 的磁盘占用）" : "缺省跟随上方分辨率偏好的第一位")
                        .font(.caption).foregroundStyle(Theme.textFaint)
                    Divider().overlay(Color.white.opacity(0.06))
                    Toggle(isOn: $upgradeKeepOld) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text("洗到新版本后保留旧版本").font(.subheadline).foregroundStyle(Theme.textMuted)
                            Text("多版本共存（收藏家模式）；关闭 = 旧版本进回收站保留 7 天").font(.caption).foregroundStyle(Theme.textFaint)
                        }
                    }
                    Divider().overlay(Color.white.opacity(0.06))
                    Button {
                        withAnimation(.snappy) { ladderOpen.toggle() }
                    } label: {
                        HStack {
                            Text("洗版优先级").font(.subheadline).foregroundStyle(Theme.textMuted)
                            Spacer()
                            Text(upgradeLadder.filter { !ladderUnconfigured($0) }.map { dim in RuleSetVocabulary.ladderOptions.first { $0.value == dim }?.label ?? dim }.joined(separator: " › "))
                                .font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                            Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.textFaint).rotationEffect(.degrees(ladderOpen ? 90 : 0))
                        }
                        .contentShape(.rect)
                    }
                    .buttonStyle(.plain)
                    if ladderOpen {
                        Text("点击依次选择").font(.caption).foregroundStyle(Theme.textFaint)
                        chips {
                            ForEach(RuleSetVocabulary.ladderOptions, id: \.value) { dim in
                                let index = upgradeLadder.firstIndex(of: dim.value)
                                SubsToggleChip(
                                    label: dim.label,
                                    active: index != nil,
                                    order: index != nil && upgradeLadder.count > 1 ? (index ?? 0) + 1 : nil,
                                    suffix: index != nil && ladderUnconfigured(dim.value) ? "· 未配置" : nil
                                ) {
                                    // 最后一维不允许移除：空阶梯与界面口径不一致
                                    if let index {
                                        if upgradeLadder.count > 1 { upgradeLadder.remove(at: index) }
                                    } else {
                                        upgradeLadder.append(dim.value)
                                    }
                                }
                            }
                        }
                        Text("按顺序逐维度比较，先分出高低的那一维说了算。每多一维，就多一轮潜在的重复下载——缺省只比分辨率与片源。标「未配置」的维度会被自动跳过；全被跳过时按缺省的「分辨率 › 片源」比。")
                            .font(.caption).foregroundStyle(Theme.textFaint)
                    }
                    let effective = upgradeLadder.filter { !ladderUnconfigured($0) }
                    if let index = effective.firstIndex(of: "platform"), index < effective.count - 1 {
                        SubsNotice(text: "很多资源的标题里根本没写平台。把平台排在前面，等于要求「先比平台再比别的」——没写平台的资源就全都分不出高低，洗版会大面积停住。建议把平台放到最后一位。", tone: .warn)
                    }
                }
                .padding(14)
                .background(Color.white.opacity(0.03), in: .rect(cornerRadius: 14))
            }
        }
    }

    /// 洗版阶梯预览：终点在上、过渡档降序排开；默认折叠只留一行终点
    private func ladderPreviewView(_ preview: UpgradeLadderPreview) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider().overlay(Color.white.opacity(0.06))
            Button {
                withAnimation(.snappy) { ladderPreviewOpen.toggle() }
            } label: {
                HStack {
                    Text("再一级级洗到 \(Text(preview.target).bold().foregroundStyle(.white))").foregroundStyle(Theme.textMuted).font(.subheadline)
                    Spacer()
                    Text(ladderPreviewOpen ? "收起" : "看会经过哪些档").font(.caption).foregroundStyle(Theme.textFaint)
                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.textFaint).rotationEffect(.degrees(ladderPreviewOpen ? 90 : 0))
                }
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            if ladderPreviewOpen {
                Text("按「\(preview.dimensions.joined(separator: " › "))」逐维比较，先分出高低的那一维说了算。").font(.caption).foregroundStyle(Theme.textFaint)
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 8) {
                        Text("终点").font(.caption2.weight(.semibold)).foregroundStyle(.white)
                            .padding(.horizontal, 8).padding(.vertical, 2).background(Theme.accent2.opacity(0.25), in: .capsule)
                        Text(preview.target).font(.subheadline.weight(.semibold)).foregroundStyle(.white)
                    }
                    Text("到手即停，不再洗版" + (preview.ceiling.map { "；比它更高的档（最高 \($0)）同样算达标，也会停" } ?? "") + "。" + (upgradeKeepOld ? "旧版本保留共存。" : "旧版本进回收站保留 7 天。"))
                        .font(.caption).foregroundStyle(Theme.textFaint)
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Theme.accent2.opacity(0.14), in: .rect(cornerRadius: 10))
                .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.accent2.opacity(0.45)))
                if preview.below.isEmpty {
                    Text("终点已是最低档，不会有过渡版本。").font(.caption).foregroundStyle(Theme.textFaint)
                } else {
                    Text("到终点之前，先到手的可能是下面任意一档；之后每遇到更高的一档，就再下一次、换掉旧的：").font(.caption).foregroundStyle(Theme.textFaint)
                    ForEach(preview.below, id: \.self) { label in
                        HStack(spacing: 8) {
                            Text("↑").foregroundStyle(Theme.textFaint)
                            Text(label).foregroundStyle(Theme.textMuted)
                        }
                        .font(.subheadline)
                    }
                    if preview.moreBelow > 0 {
                        Text("↑ 更低的 \(preview.moreBelow) 档同理").font(.caption).foregroundStyle(Theme.textFaint)
                    }
                }
            }
        }
    }

    // MARK: 积木

    private func field<Content: View>(_ label: String, hint: String? = nil, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(label).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.85))
            content()
            if let hint { Text(hint).font(.caption).foregroundStyle(Theme.textFaint).fixedSize(horizontal: false, vertical: true) }
        }
    }

    private func chips<Content: View>(@ViewBuilder _ content: () -> Content) -> some View {
        DiscoverFlowLayout(spacing: 6, lineSpacing: 6) { content() }
    }

    /// 可折叠分段：折叠头直接显示当前摘要
    private func collapsible<Content: View>(_ title: String, key: String, summary: String, @ViewBuilder content: () -> Content) -> some View {
        let open = openSections.contains(key)
        return VStack(alignment: .leading, spacing: 0) {
            Button {
                withAnimation(.snappy) {
                    if open { openSections.remove(key) } else { openSections.insert(key) }
                }
            } label: {
                HStack(spacing: 10) {
                    Text(title).font(.subheadline.weight(.semibold)).foregroundStyle(Theme.text.opacity(0.85))
                    Spacer(minLength: 8)
                    Text(summary).font(.caption).foregroundStyle(Theme.textFaint).lineLimit(1)
                    Image(systemName: "chevron.right").font(.caption).foregroundStyle(Theme.textFaint).rotationEffect(.degrees(open ? 90 : 0))
                }
                .padding(14)
                .contentShape(.rect)
            }
            .buttonStyle(.plain)
            .accessibilityIdentifier("ruleset-section-\(key)")
            if open {
                Divider().overlay(Color.white.opacity(0.06))
                VStack(alignment: .leading, spacing: 18) { content() }.padding(14)
            }
        }
        .background(Color.white.opacity(0.02), in: .rect(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(Color.white.opacity(0.07)))
    }

    private func textInput(_ placeholder: String, text: Binding<String>) -> some View {
        TextField(placeholder, text: text)
            .textFieldStyle(.plain)
            .textInputAutocapitalization(.never)
            .autocorrectionDisabled()
            .padding(.horizontal, 14).padding(.vertical, 11)
            .background(Color.white.opacity(0.04), in: .rect(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).strokeBorder(Color.white.opacity(0.08)))
    }

    private func numberInput(text: Binding<String>) -> some View {
        textInput("不限", text: text).keyboardType(.numberPad)
    }

    private func toggle(_ list: inout [String], _ value: String) {
        if let index = list.firstIndex(of: value) { list.remove(at: index) } else { list.append(value) }
    }

    private func triState(_ value: String, allow: [String], block: [String]) -> SubsTriChip.Value {
        allow.contains(value) ? .include : block.contains(value) ? .exclude : .off
    }

    /// 三态循环：不限 → 只要 → 排除 → 不限（同一值不可能同时进白黑名单）
    private func cycle(_ value: String, allow: inout [String], block: inout [String]) {
        if allow.contains(value) {
            allow.removeAll { $0 == value }
            block.append(value)
        } else if block.contains(value) {
            block.removeAll { $0 == value }
        } else {
            allow.append(value)
        }
    }

    // MARK: 数据

    private func populate() {
        let spec = ruleSet?.typedSpec ?? template?.spec ?? RuleSetSpec()
        original = spec
        name = ruleSet?.name ?? template?.name ?? ""
        // 适用范围不随复制带过去：两个组范围一模一样时只有更早的那个会被选中
        scope = RuleSetScope(ruleSet?.matchRules ?? [])
        resolutions = spec.resolutions
        mediaSources = spec.mediaSources
        codecFamilies = Set(RuleSetVocabulary.codecFamilies.filter { $0.values.contains(where: spec.videoCodecs.contains) }.map(\.label))
        platformsAllow = spec.platforms.filter(RuleSetVocabulary.platformOptions.contains)
        platformsBlock = spec.platformsBlock.filter(RuleSetVocabulary.platformOptions.contains)
        let hdr = spec.effectiveHdr
        hdrLevels = hdr.levels
        hdrBlock = hdr.block
        subLangs = spec.subtitleLanguages
        audioLangs = spec.audioLanguages
        freeOnly = spec.freeOnly
        excludeHr = spec.excludeHr
        hrStrict = spec.hrUnknownPolicy == "strict"
        minSeeders = spec.minSeeders.map(String.init) ?? ""
        sizeMin = spec.sizeMinMb.map(String.init) ?? ""
        sizeMax = spec.sizeMaxMb.map(String.init) ?? ""
        groupsAllow = spec.releaseGroupsAllow.joined(separator: ", ")
        groupsBlock = spec.releaseGroupsBlock.joined(separator: ", ")
        upgradeSource = spec.upgradeSource ?? ""
        upgradeKeepOld = spec.upgradeKeepOld
        cutoffResolution = spec.cutoffResolution ?? ""
        upgradeLadder = spec.upgradeLadder ?? RuleSetVocabulary.defaultLadder
    }

    private func submit() async {
        let draft = draft
        if let message = draft.error {
            error = message
            return
        }
        busy = true
        error = nil
        let payload = API.RuleSetPayload(
            name: name.trimmingCharacters(in: .whitespaces),
            spec: draft.spec.json,
            matchRules: validScope.rules
        )
        do {
            let saved = if let ruleSet {
                try await api.rulesUpdate(ruleSetId: ruleSet.id, body: payload)
            } else {
                try await api.rulesCreate(body: payload)
            }
            onSaved(saved)
            dismiss()
        } catch {
            self.error = error.localizedDescription.isEmpty ? "保存失败，请稍后重试" : error.localizedDescription
            busy = false
        }
    }
}
