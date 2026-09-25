import Foundation

/// 媒体库首页的「行清单」：合并规则、排序预设与命名推荐。逐行移植自 Web `lib/home-rows.ts`。
///
/// 首页 = 每个成员一份有序的行清单；每一行 = 来源 × 排序 × 名字。清单存在 `ui.preferences.home.rows`。
/// 约定：**存下来的是提示，不是契约**——
/// - 存过的行按存的顺序在前；
/// - 没存过的内置行、每个可见库的默认行按出厂顺序补在后面（新版本加的内置行、新建的库一定看得到）；
/// - 指向已删库 / 不可见合集的行直接忽略；
/// - 空清单 = 出厂布局，「恢复默认」就是存一个空列表。
///
/// 与 Web 共用同一份偏好，两端合并逻辑必须一致，否则同一账号两端看到的首页不同。
enum HomeRows {
    // MARK: 排序预设

    /// 库行 / 合集行可选的排序档（取值与海报墙同名，另含首页独有的 random）
    static let allSorts = ["added_at", "release_date", "last_played", "rating", "random", "title"]

    struct SortPreset {
        /// 推荐的行名（按方向）
        var name: (_ library: String, _ reversed: Bool) -> String
        /// 不带库名的短标签（按方向）
        var short: (_ reversed: Bool) -> String
        var direction: WallSortDirection?
        var hint: String
    }

    static func preset(_ sort: String) -> SortPreset {
        switch sort {
        case "release_date":
            SortPreset(name: { l, r in r ? "最早上映的\(l)" : "最近上映的\(l)" }, short: { $0 ? "最早上映" : "最近上映" },
                       direction: WallSortDirection(naturalAsc: false, asc: "旧→新", desc: "新→旧"), hint: "上映时间")
        case "last_played":
            // 反转 = 上次播放离现在最远的在前：「很久没看的」比「最早观看的」更像人话
            SortPreset(name: { l, r in r ? "很久没看的\(l)" : "最近观看的\(l)" }, short: { $0 ? "很久没看" : "最近观看" },
                       direction: WallSortDirection(naturalAsc: false, asc: "远→近", desc: "近→远"), hint: "我播放过的，按上次播放时间")
        case "rating":
            SortPreset(name: { l, r in r ? "评分最低的\(l)" : "评分最高的\(l)" }, short: { $0 ? "评分最低" : "评分最高" },
                       direction: WallSortDirection(naturalAsc: false, asc: "低→高", desc: "高→低"), hint: "评分")
        case "random":
            SortPreset(name: { l, _ in "随便看看 · \(l)" }, short: { _ in "随便看看" }, direction: nil, hint: "每天换一批")
        case "title":
            SortPreset(name: { l, r in r ? "\(l) Z–A" : "\(l) A–Z" }, short: { $0 ? "Z–A" : "A–Z" },
                       direction: WallSortDirection(naturalAsc: true, asc: "A→Z", desc: "Z→A"), hint: "片名")
        default: // added_at
            SortPreset(name: { l, r in r ? "最早添加的\(l)" : "最近添加的\(l)" }, short: { $0 ? "最早添加" : "最近添加" },
                       direction: WallSortDirection(naturalAsc: false, asc: "旧→新", desc: "新→旧"), hint: "入库时间")
        }
    }

    /// 排序预设按库类型裁剪：评分、上映对家庭录像与照片没有意义
    static func sorts(for kind: String) -> [String] {
        switch kind {
        case "photo": ["added_at", "title", "random"]
        case "video": ["added_at", "last_played", "title", "random"]
        default: allSorts
        }
    }

    /// 「我的收藏」行的排序档
    static let favoritesSorts = ["unwatched_first", "favorited_at", "rating", "title"]

    struct FavoritesPreset {
        var name: (_ reversed: Bool) -> String
        var direction: WallSortDirection?
        var hint: String
    }

    static func favoritesPreset(_ sort: String) -> FavoritesPreset {
        switch sort {
        case "favorited_at":
            FavoritesPreset(name: { $0 ? "最早收藏" : "最近收藏" }, direction: WallSortDirection(naturalAsc: false, asc: "旧→新", desc: "新→旧"), hint: "收藏时间")
        case "rating":
            FavoritesPreset(name: { $0 ? "评分最低" : "评分最高" }, direction: WallSortDirection(naturalAsc: false, asc: "低→高", desc: "高→低"), hint: "评分")
        case "title":
            FavoritesPreset(name: { $0 ? "片名 Z–A" : "片名 A–Z" }, direction: WallSortDirection(naturalAsc: true, asc: "A→Z", desc: "Z→A"), hint: "片名")
        default:
            FavoritesPreset(name: { _ in "未看优先" }, direction: nil, hint: "没看完的在前，再按收藏时间")
        }
    }

    // MARK: 行模型

    enum Kind: Hashable {
        case upNext
        case favorites(sort: String, reversed: Bool)
        case libraries
        case library(library: API.LibraryView, sort: String, reversed: Bool, unwatched: Bool, name: String, builtin: Bool)
        case collection(collection: API.CollectionView, sort: String, reversed: Bool, name: String)
    }

    /// 合并后的一行：来源已解析、排序与名字已落到具体值，首页与自定义页直接消费
    struct Row: Hashable, Identifiable {
        var id: String
        var hidden: Bool
        var kind: Kind

        var sort: String? {
            switch kind {
            case let .favorites(sort, _), let .library(_, sort, _, _, _, _), let .collection(_, sort, _, _): sort
            default: nil
            }
        }

        var reversed: Bool {
            switch kind {
            case let .favorites(_, r), let .library(_, _, r, _, _, _), let .collection(_, _, r, _): r
            default: false
            }
        }

        /// 这一行显示的名字：用户起的优先，空则跟随默认（库行按排序推荐，合集行用合集名）
        var title: String {
            switch kind {
            case .upNext: "接下来继续"
            case .favorites: "我的收藏"
            case .libraries: "我的媒体库"
            case let .library(library, sort, reversed, _, name, _):
                name.isEmpty ? HomeRows.preset(sort).name(library.name, reversed) : name
            case let .collection(collection, _, _, name):
                name.isEmpty ? collection.name : name
            }
        }

        /// 自定义页里每行的小字：来源 · 排序 · 只看没看过的
        var meta: String {
            switch kind {
            case .upNext: "内置 · 我正在看的"
            case let .favorites(sort, reversed): "内置 · \(HomeRows.favoritesPreset(sort).name(reversed))"
            case .libraries: "内置 · 管理页的库顺序"
            case let .library(library, sort, reversed, unwatched, _, _):
                ["\(library.name)库", HomeRows.preset(sort).short(reversed), unwatched ? "只看没看过的" : nil]
                    .compactMap { $0 }.joined(separator: " · ")
            case let .collection(_, sort, reversed, _): "合集 · \(HomeRows.preset(sort).short(reversed))"
            }
        }

        /// 库行 / 合集行的默认名（改名输入框的占位）
        var defaultTitle: String {
            switch kind {
            case let .library(library, sort, reversed, _, _, _): HomeRows.preset(sort).name(library.name, reversed)
            case let .collection(collection, _, _, _): collection.name
            default: title
            }
        }

        var customName: String {
            switch kind {
            case let .library(_, _, _, _, name, _), let .collection(_, _, _, name): name
            default: ""
            }
        }

        /// 能否删除：只有自加的行（row:）能删，内置行与每库默认行只能藏
        var removable: Bool { id.hasPrefix("row:") }
    }

    // MARK: 合并

    /// 出厂布局：接下来继续 → 我的收藏 → 我的媒体库 → 每个库一行「最近添加」
    private static func defaultRows(_ libraries: [API.LibraryView]) -> [Row] {
        [
            Row(id: "up-next", hidden: false, kind: .upNext),
            Row(id: "favorites", hidden: false, kind: .favorites(sort: "unwatched_first", reversed: false)),
            Row(id: "libraries", hidden: false, kind: .libraries),
        ] + libraries.filter { !$0.excludeFromHome }.map {
            Row(id: "lib:\($0.id)", hidden: false, kind: .library(library: $0, sort: "added_at", reversed: false, unwatched: false, name: "", builtin: true))
        }
    }

    /// 存下来的（sort, order）→（档位, 是否反转）；老偏好的 release_date_asc 归一成 release_date + 反转
    private static func asRowSort(_ value: String?, _ order: String?, allowed: [String]) -> (String, Bool) {
        if value == "release_date_asc", allowed.contains("release_date") { return ("release_date", true) }
        let sort = value.flatMap { allowed.contains($0) ? $0 : nil } ?? allowed[0]
        return (sort, preset(sort).direction?.isReversed(order: order) ?? false)
    }

    /// 把存下来的清单与当前可见的库、合集合并成首页要渲染的行
    static func build(prefs: [API.HomeRowPref], libraries: [API.LibraryView], collections: [API.CollectionView]) -> [Row] {
        let visible = libraries.filter(\.viewerAccess)
        let defaults = defaultRows(visible)
        if prefs.isEmpty { return defaults }
        let libById = Dictionary(visible.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        let colById = Dictionary(collections.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        var seen = Set<String>()
        var rows: [Row] = []
        for pref in prefs where !seen.contains(pref.id) {
            guard let row = resolve(pref, libById, colById) else { continue }
            seen.insert(pref.id)
            rows.append(row)
        }
        // 没存过的内置行追加在末尾（版本升级新增的入口不能消失）
        for row in defaults {
            if case .library = row.kind { continue }
            if seen.contains(row.id) { continue }
            seen.insert(row.id)
            rows.append(row)
        }
        // 没存过的库补一条默认行，插在最后一条库行之后（没有库行时插在「我的媒体库」之后）
        let missing = defaults.filter { row in
            if case .library = row.kind { return !seen.contains(row.id) }
            return false
        }
        if !missing.isEmpty {
            var at = rows.count
            if let last = rows.lastIndex(where: { if case .library = $0.kind { true } else { false } }) {
                at = last + 1
            } else if let libs = rows.firstIndex(where: { $0.kind == .libraries }) {
                at = libs + 1
            }
            rows.insert(contentsOf: missing, at: at)
        }
        return rows
    }

    private static func resolve(_ pref: API.HomeRowPref, _ libById: [Int: API.LibraryView], _ colById: [Int: API.CollectionView]) -> Row? {
        let hidden = pref.hidden == true
        switch pref.id {
        case "up-next": return Row(id: "up-next", hidden: hidden, kind: .upNext)
        case "libraries": return Row(id: "libraries", hidden: hidden, kind: .libraries)
        case "favorites":
            let sort = favoritesSorts.contains(pref.sort ?? "") ? pref.sort! : "unwatched_first"
            let reversed = favoritesPreset(sort).direction?.isReversed(order: pref.order) ?? false
            return Row(id: "favorites", hidden: hidden, kind: .favorites(sort: sort, reversed: reversed))
        default: break
        }
        if pref.id.hasPrefix("lib:") {
            // 管理员勾了「从首页排除」的库：默认行不出现，存过也一样
            guard let id = Int(pref.id.dropFirst(4)), let library = libById[id], !library.excludeFromHome else { return nil }
            return libraryRow(pref, library, builtin: true)
        }
        guard pref.id.hasPrefix("row:") else { return nil }
        if let cid = pref.collectionId {
            guard let collection = colById[cid] else { return nil }
            // 没存排序时沿用合集自己的序（含方向）
            let (sort, reversed) = pref.sort != nil
                ? asRowSort(pref.sort, pref.order, allowed: allSorts)
                : asRowSort(collection.sort, nil, allowed: allSorts)
            return Row(id: pref.id, hidden: hidden, kind: .collection(collection: collection, sort: sort, reversed: reversed, name: (pref.name ?? "").trimmingCharacters(in: .whitespaces)))
        }
        if let lid = pref.libraryId {
            guard let library = libById[lid] else { return nil }
            return libraryRow(pref, library, builtin: false)
        }
        return nil
    }

    private static func libraryRow(_ pref: API.HomeRowPref, _ library: API.LibraryView, builtin: Bool) -> Row {
        let (sort, reversed) = asRowSort(pref.sort, pref.order, allowed: sorts(for: library.kind))
        // 「最近观看」只要播过的，与「只看没看过的」互斥：以排序为准，开关作废
        return Row(id: pref.id, hidden: pref.hidden == true, kind: .library(
            library: library, sort: sort, reversed: reversed,
            unwatched: pref.unwatched == true && sort != "last_played",
            name: (pref.name ?? "").trimmingCharacters(in: .whitespaces), builtin: builtin
        ))
    }

    /// 反向：把合并后的行写回可存的形状。只存与默认不同的字段，空即默认
    static func toPrefs(_ rows: [Row]) -> [API.HomeRowPrefInput] {
        rows.map { row in
            var pref = API.HomeRowPrefInput(id: row.id)
            if row.hidden { pref.hidden = true }
            switch row.kind {
            case let .favorites(sort, reversed):
                if sort != "unwatched_first" { pref.sort = sort }
                pref.order = favoritesPreset(sort).direction?.orderParam(reversed: reversed)
            case let .library(library, sort, reversed, unwatched, name, builtin):
                if !builtin { pref.libraryId = library.id }
                pref.sort = sort
                pref.order = preset(sort).direction?.orderParam(reversed: reversed)
                if unwatched { pref.unwatched = true }
                if !name.isEmpty { pref.name = name }
            case let .collection(collection, sort, reversed, name):
                pref.collectionId = collection.id
                pref.sort = sort
                pref.order = preset(sort).direction?.orderParam(reversed: reversed)
                if !name.isEmpty { pref.name = name }
            default: break
            }
            return pref
        }
    }

    /// 新加的行用随机 id：`row:` + 6 位 base36
    static func newRowId() -> String {
        let chars = Array("0123456789abcdefghijklmnopqrstuvwxyz")
        return "row:" + String((0 ..< 6).map { _ in chars.randomElement()! })
    }

    static func newLibraryRow(_ library: API.LibraryView) -> Row {
        Row(id: newRowId(), hidden: false, kind: .library(library: library, sort: "added_at", reversed: false, unwatched: false, name: "", builtin: false))
    }

    static func newCollectionRow(_ collection: API.CollectionView) -> Row {
        let (sort, reversed) = asRowSort(collection.sort, nil, allowed: allSorts)
        return Row(id: newRowId(), hidden: false, kind: .collection(collection: collection, sort: sort, reversed: reversed, name: ""))
    }
}
