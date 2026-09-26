import SwiftUI

/// 排序芯片（对应 Web `OrderChips`）：有序优先级的统一交互。
///
/// - **候选区只管「加」**：点常用芯片加入优先级（按点击顺序排在末尾），再点移除；
///   达到上限后未选中的芯片变灰不可点；至少保留一项（最后一项点不掉）。
/// - **长尾项走「更多」面板**：全量语种/地区表来自后端，Web 是行内展开的搜索面板，
///   手机上改成带搜索框的弹层（最多列 60 条，与 Web 同口径），选中即关。
/// - **已选序列条管顺序与删除**：每项带「↑ 上移」与「✕ 移除」，首位可挂角色标签（主语言 / 首选）。
///   没有它的话想把第 2 位提到第 1 位得「先移除再重加」——两步且不直观。
/// - 已选中的非常用项也出现在候选行内，否则保存过的长尾语种打开页面就「消失」了。
struct SettingsBScrapeOrderChips: View {
    let options: [SettingsBScrapeChipOption]
    let extraOptions: [SettingsBScrapeChipOption]
    /// 「更多语言」/「更多地区」
    let moreLabel: String
    @Binding var value: [String]
    let max: Int
    /// 首位角色标签（"主语言" / "首选"），空串只显示顺序
    let primaryTag: String
    /// 测试标识前缀（如 scrape-meta-lang）
    let identifier: String

    @State private var moreOpen = false

    /// 行内候选 = 常用项 + 已选中的长尾项
    private var inline: [SettingsBScrapeChipOption] {
        let known = Set(options.map(\.id))
        let extras = value.filter { !known.contains($0) }.map { id in
            extraOptions.first { $0.id == id } ?? SettingsBScrapeChipOption(id: id, name: id)
        }
        return options + extras
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            SettingsBFlow(spacing: 8, lineSpacing: 8) {
                ForEach(inline, id: \.id) { option in
                    candidate(option)
                }
                if !extraOptions.isEmpty {
                    Button {
                        moreOpen = true
                    } label: {
                        HStack(spacing: 5) {
                            Text("…")
                                .font(.caption2.weight(.bold))
                                .frame(width: 18, height: 18)
                                .background(Color.white.opacity(0.1), in: .circle)
                            Text("更多\(moreLabel)").font(.footnote)
                        }
                        .padding(.leading, 5)
                        .padding(.trailing, 11)
                        .padding(.vertical, 5)
                        .foregroundStyle(Theme.textFaint)
                        .overlay(Capsule().strokeBorder(Color.white.opacity(0.15), style: StrokeStyle(lineWidth: 1, dash: [3, 3])))
                        .contentShape(.capsule)
                    }
                    .buttonStyle(.plain)
                    .accessibilityIdentifier("\(identifier)-more")
                }
            }

            // 特殊 token 的说明：Web 是悬停提示，手机没有悬停，直接写在下面
            let tips = options.filter { $0.tip != nil }
            if !tips.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(tips, id: \.id) { option in
                        Text("\(Text(option.name).foregroundStyle(Theme.textMuted))：\(option.tip ?? "")")
                            .font(.caption)
                            .foregroundStyle(Theme.textFaint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }

            if value.isEmpty {
                Text("点击上方候选加入，加入后可在这里排序")
                    .font(.caption)
                    .foregroundStyle(Theme.textFaint)
            } else {
                SettingsBFlow(spacing: 6, lineSpacing: 6) {
                    ForEach(Array(value.enumerated()), id: \.element) { index, id in
                        selectedPill(index: index, id: id)
                    }
                    Text("按此顺序回落（最多 \(max) 项）")
                        .font(.caption)
                        .foregroundStyle(Theme.textFaint)
                        .padding(.vertical, 5)
                }
            }
        }
        .padding(.vertical, 4)
        .sheet(isPresented: $moreOpen) {
            SettingsBScrapeMoreSheet(
                moreLabel: moreLabel,
                candidates: extraOptions.filter { e in !inline.contains { $0.id == e.id } },
                atCapacity: value.count >= max
            ) { id in
                toggle(id)
            }
            .sheetFeedback()
        }
    }

    private func candidate(_ option: SettingsBScrapeChipOption) -> some View {
        let selected = value.contains(option.id)
        let disabled = !selected && value.count >= max
        return Button {
            toggle(option.id)
        } label: {
            HStack(spacing: 6) {
                Text(selected ? "✓" : "+")
                    .font(.caption2.weight(.bold))
                    .frame(width: 18, height: 18)
                    .foregroundStyle(selected ? Color(red: 0x12 / 255, green: 0x14 / 255, blue: 0x1C / 255) : Theme.textFaint)
                    .background(selected ? Theme.accent : Color.white.opacity(0.1), in: .circle)
                Text(option.name).font(.footnote.weight(.medium))
            }
            .padding(.leading, 5)
            .padding(.trailing, 11)
            .padding(.vertical, 5)
            .foregroundStyle(selected ? Theme.text : Theme.textMuted)
            .background(selected ? Theme.accentSoft : Color.white.opacity(0.04), in: .capsule)
            .overlay(Capsule().strokeBorder(selected ? Theme.accent.opacity(0.7) : Color.white.opacity(0.08)))
        }
        .buttonStyle(.plain)
        .disabled(disabled)
        .opacity(disabled ? 0.35 : 1)
        .accessibilityIdentifier("\(identifier)-chip-\(option.id)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private func selectedPill(index: Int, id: String) -> some View {
        let name = inline.first { $0.id == id }?.name ?? id
        return HStack(spacing: 4) {
            if index == 0, !primaryTag.isEmpty {
                Text(primaryTag).font(.caption2).foregroundStyle(Theme.accent)
            }
            Text(name).font(.footnote).foregroundStyle(Theme.text)
            Button {
                var next = value
                next.swapAt(index - 1, index)
                value = next
            } label: {
                Image(systemName: "arrow.up").font(.caption2.weight(.semibold)).frame(width: 22, height: 22).contentShape(.rect)
            }
            .disabled(index == 0)
            .opacity(index == 0 ? 0.25 : 1)
            .accessibilityLabel("把「\(name)」上移一位")
            .accessibilityIdentifier("\(identifier)-up-\(id)")
            Button {
                value.removeAll { $0 == id }
            } label: {
                Image(systemName: "xmark").font(.caption2.weight(.semibold)).frame(width: 22, height: 22).contentShape(.rect)
            }
            .disabled(value.count <= 1)
            .opacity(value.count <= 1 ? 0.25 : 1)
            .accessibilityLabel("移除「\(name)」")
            .accessibilityIdentifier("\(identifier)-remove-\(id)")
        }
        .buttonStyle(.plain)
        .foregroundStyle(Theme.textMuted)
        .padding(.leading, 10)
        .padding(.trailing, 3)
        .padding(.vertical, 2)
        .background(Theme.accentSoft, in: .capsule)
        .overlay(Capsule().strokeBorder(Theme.accent.opacity(0.3)))
    }

    private func toggle(_ id: String) {
        if let index = value.firstIndex(of: id) {
            guard value.count > 1 else { return } // 至少保留一项
            value.remove(at: index)
        } else {
            guard value.count < max else { return }
            value.append(id)
        }
    }
}

/// 「更多语言 / 更多地区」搜索弹层：按名称或代码过滤，最多列 60 条，点选即加入并关闭
private struct SettingsBScrapeMoreSheet: View {
    let moreLabel: String
    let candidates: [SettingsBScrapeChipOption]
    /// 已达上限：Web 在这种情况下点了也不会加入，这里提前说明
    let atCapacity: Bool
    let onPick: (String) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    private var filtered: [SettingsBScrapeChipOption] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        return Array(candidates.filter { q.isEmpty || $0.name.lowercased().contains(q) || $0.id.lowercased().contains(q) }.prefix(60))
    }

    var body: some View {
        NavigationStack {
            List {
                if atCapacity {
                    Text("已达上限，请先在已选序列里移除一项").font(.footnote).foregroundStyle(Theme.warning)
                }
                if filtered.isEmpty {
                    Text("没有匹配的\(moreLabel)").foregroundStyle(Theme.textFaint)
                } else {
                    ForEach(filtered, id: \.id) { option in
                        Button {
                            onPick(option.id)
                            dismiss()
                        } label: {
                            HStack {
                                Text(option.name).foregroundStyle(Theme.text)
                                Spacer()
                                Text(option.id).font(.caption.monospaced()).foregroundStyle(Theme.textFaint)
                            }
                        }
                        .disabled(atCapacity)
                        .accessibilityIdentifier("scrape-more-option-\(option.id)")
                    }
                }
            }
            .scrollContentBackground(.hidden)
            .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "搜索\(moreLabel)（名称或代码）…")
            .navigationTitle("更多\(moreLabel)")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("关闭", systemImage: "xmark") { dismiss() }
                }
            }
        }
        .presentationBackground(.regularMaterial)
    }
}
