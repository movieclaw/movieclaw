// 由 ios/MovieClaw/scripts/gen_api.py 生成，勿手改。重新生成见脚本头部说明。
import Foundation

nonisolated extension APIClient {
    /// 为一条待处理事项生成交给 Agent 的诊断工单
    /// `POST /agent-handoff`
    func sessionHandoffPrompt(body: API.HandoffRequest) async throws -> API.HandoffPromptView {
        return try await send("POST", "/agent-handoff", body: body)
    }

    /// 读取应用设置
    /// `GET /app/config`
    func appShow() async throws -> API.AppConfigView {
        return try await send("GET", "/app/config")
    }

    /// 保存应用设置（即时生效）
    /// `PUT /app/config`
    func appSet(body: API.AppConfigPayload) async throws -> API.AppConfigView {
        return try await send("PUT", "/app/config", body: body)
    }

    /// 修改对外端口（保存后全量重启生效）
    /// `PUT /app/port`
    func appPortSet(body: API.WebPortPayload) async throws -> API.AppConfigView {
        return try await send("PUT", "/app/port", body: body)
    }

    /// 重启应用（优雅停机后由容器入口/进程守护拉起）
    /// `POST /app/restart`
    func appRestart() async throws -> Void {
        let _: API.JSONValue? = try await send("POST", "/app/restart")
    }

    /// 运行期数据目录的占用统计
    /// `GET /app/storage`
    func appStorageUsage(refresh: Bool? = nil) async throws -> API.StorageStateView {
        var query: [URLQueryItem] = []
        if let refresh { query.append(URLQueryItem(name: "refresh", value: "\(refresh)")) }
        return try await send("GET", "/app/storage", query: query)
    }

    /// 清理某个缓存目录
    /// `POST /app/storage/{key}/clean`
    func appStorageClean(key: String, body: API.CleanPayload) async throws -> API.CleanResultView {
        return try await send("POST", "/app/storage/\(key)/clean", body: body)
    }

    /// 应用最新版本（下载校验后自动重启生效）
    /// `POST /app/update/apply`
    func appUpdateApply() async throws -> API.UpdateProgressView {
        return try await send("POST", "/app/update/apply")
    }

    /// 检查是否有新版本（比对 GitHub 最新 Release）
    /// `POST /app/update/check`
    func appUpdateCheck() async throws -> API.UpdateCheckView {
        return try await send("POST", "/app/update/check")
    }

    /// 确认上一次异常退出告警（清除记录，不再展示）
    /// `POST /app/update/last-exit/dismiss`
    func appUpdateLastExitDismiss() async throws -> Void {
        let _: API.JSONValue? = try await send("POST", "/app/update/last-exit/dismiss")
    }

    /// 更新 NER 模型（下载校验后重启后端生效）
    /// `POST /app/update/model/apply`
    func appUpdateModelApply() async throws -> API.UpdateProgressView {
        return try await send("POST", "/app/update/model/apply")
    }

    /// 检查 NER 模型是否有新版本
    /// `POST /app/update/model/check`
    func appUpdateModelCheck() async throws -> API.ModelUpdateCheckView {
        return try await send("POST", "/app/update/model/check")
    }

    /// 读取待更新快照（最近一次检查的结论，不触网）
    /// `GET /app/update/pending`
    func appUpdatePending() async throws -> API.PendingUpdateView {
        return try await send("GET", "/app/update/pending")
    }

    /// 读取更新执行进度
    /// `GET /app/update/progress`
    func appUpdateProgress() async throws -> API.UpdateProgressView {
        return try await send("GET", "/app/update/progress")
    }

    /// 设置本地保留的版本目录数（立即按新策略清理）
    /// `PUT /app/update/retention`
    func appUpdateRetention(body: API.UpdateRetentionPayload) async throws -> Void {
        let _: API.JSONValue? = try await send("PUT", "/app/update/retention", body: body)
    }

    /// 回退到指定版本/镜像内置版本（不带 target 时回退上一版本）
    /// `POST /app/update/rollback`
    func appUpdateRollback(body: API.RollbackPayload? = nil) async throws -> Void {
        let _: API.JSONValue? = try await send("POST", "/app/update/rollback", body: body)
    }

    /// 回退选择器数据：本地保留的历史版本、数据兼容判定与保留策略
    /// `GET /app/update/rollback/options`
    func appUpdateRollbackOptions() async throws -> API.RollbackOptionsView {
        return try await send("GET", "/app/update/rollback/options")
    }

    /// 读取应用版本与更新能力状态
    /// `GET /app/update/status`
    func appUpdateStatus() async throws -> API.UpdateStatusView {
        return try await send("GET", "/app/update/status")
    }

    /// 读取外观设置（背景图库与当前生效图）
    /// `GET /appearance`
    func appearanceShow() async throws -> API.AppearanceView {
        return try await send("GET", "/appearance")
    }

    /// 切换当前生效的背景图
    /// `PUT /appearance/active`
    func appearanceActiveSet(body: API.ActiveBackdropUpdate) async throws -> API.AppearanceView {
        return try await send("PUT", "/appearance/active", body: body)
    }

    /// 从图库删除一张背景图
    /// `DELETE /appearance/backdrops/{backdrop_id}`
    func appearanceBackdropsDelete(backdropId: String) async throws -> API.AppearanceView {
        return try await send("DELETE", "/appearance/backdrops/\(backdropId)")
    }

    /// 列出本浏览器已登录的全部账号（激活账号排第一）
    /// `GET /auth/accounts`
    func authAccountsList() async throws -> [API.AccountView] {
        return try await send("GET", "/auth/accounts")
    }

    /// 切换到本浏览器已登录的另一个账号（无需再输密码）
    /// `POST /auth/accounts/switch`
    func authAccountsSwitch(body: API.SwitchAccountRequest) async throws -> API.SessionView {
        return try await send("POST", "/auth/accounts/switch", body: body)
    }

    /// 从本浏览器移除一个已登录账号（移除的是当前账号时自动切到下一个）
    /// `DELETE /auth/accounts/{username}`
    func authAccountsRemove(username: String) async throws -> API.SessionView? {
        return try await send("DELETE", "/auth/accounts/\(username)")
    }

    /// 查询系统是否已完成首次初始化
    /// `GET /auth/bootstrap`
    func authBootstrapStatus() async throws -> API.BootstrapStatus {
        return try await send("GET", "/auth/bootstrap")
    }

    /// 首次初始化：创建超级管理员（全生命周期仅一次）
    /// `POST /auth/bootstrap`
    func authBootstrapCreate(body: API.BootstrapRequest) async throws -> API.SessionView {
        return try await send("POST", "/auth/bootstrap", body: body)
    }

    /// 设备发起接入请求，取得配对码（匿名）
    /// `POST /auth/device/authorize`
    func authDeviceAuthorize(body: API.DeviceAuthorizeRequest) async throws -> API.DeviceAuthorizeView {
        return try await send("POST", "/auth/device/authorize", body: body)
    }

    /// 设备轮询兑换令牌（匿名）
    /// `POST /auth/device/token`
    func authDeviceToken(body: API.DeviceTokenRequest) async throws -> API.DeviceTokenView? {
        return try await send("POST", "/auth/device/token", body: body)
    }

    /// 列出待批准的设备接入请求
    /// `GET /auth/devices/requests`
    func authDevicesRequests() async throws -> [API.DeviceRequestView] {
        return try await send("GET", "/auth/devices/requests")
    }

    /// 批准一台设备接入（此刻才签发令牌）
    /// `POST /auth/devices/requests/{user_code}/approve`
    func authDevicesApprove(userCode: String) async throws -> Void {
        let _: API.JSONValue? = try await send("POST", "/auth/devices/requests/\(userCode)/approve")
    }

    /// 拒绝一台设备接入
    /// `POST /auth/devices/requests/{user_code}/deny`
    func authDevicesDeny(userCode: String) async throws -> Void {
        let _: API.JSONValue? = try await send("POST", "/auth/devices/requests/\(userCode)/deny")
    }

    /// 管理员登录
    /// `POST /auth/login`
    func authLogin(body: API.LoginRequest) async throws -> API.SessionView {
        return try await send("POST", "/auth/login", body: body)
    }

    /// 退出当前账号（自动切到浏览器里的下一个账号）；all=true 退出全部
    /// `POST /auth/logout`
    func authLogout(body: API.LogoutRequest? = nil) async throws -> API.SessionView? {
        return try await send("POST", "/auth/logout", body: body)
    }

    /// 查询当前登录状态
    /// `GET /auth/me`
    func authMe() async throws -> API.SessionView {
        return try await send("GET", "/auth/me")
    }

    /// 修改密码（本人其余会话强制下线）
    /// `PUT /auth/password`
    func authPasswordUpdate(body: API.ChangePasswordRequest) async throws -> API.SessionView {
        return try await send("PUT", "/auth/password", body: body)
    }

    /// 修改个人信息（昵称）
    /// `PUT /auth/profile`
    func authProfileUpdate(body: API.UpdateProfileRequest) async throws -> API.SessionView {
        return try await send("PUT", "/auth/profile", body: body)
    }

    /// 列出已创建的 CLI API 令牌（仅元信息，不含明文）
    /// `GET /auth/tokens`
    func authTokensList() async throws -> [API.ApiTokenView] {
        return try await send("GET", "/auth/tokens")
    }

    /// 创建 CLI API 令牌（明文仅返回这一次，请立即保存）
    /// `POST /auth/tokens`
    func authTokensCreate(body: API.ApiTokenCreateRequest) async throws -> API.ApiTokenCreatedView {
        return try await send("POST", "/auth/tokens", body: body)
    }

    /// 吊销一枚 CLI API 令牌（立即失效，不影响其他令牌）
    /// `DELETE /auth/tokens/{token_id}`
    func authTokensRevoke(tokenId: String) async throws -> Void {
        let _: API.JSONValue? = try await send("DELETE", "/auth/tokens/\(tokenId)")
    }

    /// 接入飞书群机器人(粘贴 Webhook 地址,即绑即用)
    /// `POST /channels/im/feishu/bindings`
    func channelsImFeishuBind(body: API.FeishuBindPayload) async throws -> API.ImAccountView {
        return try await send("POST", "/channels/im/feishu/bindings", body: body)
    }

    /// 读取推送内容开关
    /// `GET /channels/im/push-config`
    func channelsImPushConfigGet() async throws -> API.ChannelPushConfigView {
        return try await send("GET", "/channels/im/push-config")
    }

    /// 保存推送内容开关
    /// `PUT /channels/im/push-config`
    func channelsImPushConfigUpdate(body: API.ChannelPushConfigView) async throws -> API.ChannelPushConfigView {
        return try await send("PUT", "/channels/im/push-config", body: body)
    }

    /// 向所有已绑定通道发送测试推送
    /// `POST /channels/im/push-test`
    func channelsImPushTest(body: API.PushTestPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/channels/im/push-test", body: body)
    }

    /// 已绑定的 TG/Discord 账号列表
    /// `GET /channels/im/{channel}/accounts`
    func channelsImAccountsList(channel: String) async throws -> [API.ImAccountView] {
        return try await send("GET", "/channels/im/\(channel)/accounts")
    }

    /// 解绑 TG/Discord 账号
    /// `DELETE /channels/im/{channel}/accounts/{account_id}`
    func channelsImAccountsUnbind(channel: String, accountId: String) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/channels/im/\(channel)/accounts/\(accountId)")
    }

    /// 发起配对绑定(提交 bot token,返回配对码)
    /// `POST /channels/im/{channel}/bindings`
    func channelsImBindingsStart(channel: String, body: API.ImBindTokenPayload) async throws -> API.ImBindingView {
        return try await send("POST", "/channels/im/\(channel)/bindings", body: body)
    }

    /// 查询配对状态(前端轮询)
    /// `GET /channels/im/{channel}/bindings/{challenge_id}`
    func channelsImBindingsStatus(channel: String, challengeId: String) async throws -> API.ImBindingView {
        return try await send("GET", "/channels/im/\(channel)/bindings/\(challengeId)")
    }

    /// 已绑定的微信账号列表
    /// `GET /channels/weixin/accounts`
    func channelsWeixinAccountsList() async throws -> [API.WeixinAccountView] {
        return try await send("GET", "/channels/weixin/accounts")
    }

    /// 解绑微信账号
    /// `DELETE /channels/weixin/accounts/{account_id}`
    func channelsWeixinAccountsUnbind(accountId: String) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/channels/weixin/accounts/\(accountId)")
    }

    /// 发起微信扫码绑定
    /// `POST /channels/weixin/bindings`
    func channelsWeixinBindingsStart() async throws -> API.WeixinBindingStartView {
        return try await send("POST", "/channels/weixin/bindings")
    }

    /// 查询绑定状态(前端轮询)
    /// `GET /channels/weixin/bindings/{challenge_id}`
    func channelsWeixinBindingsStatus(challengeId: String) async throws -> API.WeixinBindingStatusView {
        return try await send("GET", "/channels/weixin/bindings/\(challengeId)")
    }

    /// 提交扫码配对数字
    /// `POST /channels/weixin/bindings/{challenge_id}/verify-code`
    func channelsWeixinBindingsVerify(challengeId: String, body: API.WeixinVerifyCodePayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/channels/weixin/bindings/\(challengeId)/verify-code", body: body)
    }

    /// 合集列表（按元数据可见性过滤，成员为空的不列）
    /// `GET /collections`
    func collectionList(libraryId: Int? = nil, includeEmpty: Bool? = nil, includeHidden: Bool? = nil) async throws -> [API.CollectionView] {
        var query: [URLQueryItem] = []
        if let libraryId { query.append(URLQueryItem(name: "library_id", value: "\(libraryId)")) }
        if let includeEmpty { query.append(URLQueryItem(name: "include_empty", value: "\(includeEmpty)")) }
        if let includeHidden { query.append(URLQueryItem(name: "include_hidden", value: "\(includeHidden)")) }
        return try await send("GET", "/collections", query: query)
    }

    /// 创建合集（筛完存为合集）
    /// `POST /collections`
    func collectionCreate(body: API.CollectionPayload) async throws -> API.CollectionView {
        return try await send("POST", "/collections", body: body)
    }

    /// 删除合集（不动作品本身）
    /// `DELETE /collections/{collection_id}`
    func collectionDelete(collectionId: Int) async throws -> Void {
        let _: API.JSONValue? = try await send("DELETE", "/collections/\(collectionId)")
    }

    /// 合集详情
    /// `GET /collections/{collection_id}`
    func collectionGet(collectionId: Int) async throws -> API.CollectionView {
        return try await send("GET", "/collections/\(collectionId)")
    }

    /// 改合集（改名 / 改规则 / 改可见性）
    /// `PUT /collections/{collection_id}`
    func collectionUpdate(collectionId: Int, body: API.CollectionPayload) async throws -> API.CollectionView {
        return try await send("PUT", "/collections/\(collectionId)", body: body)
    }

    /// 合集成员的图廊：海报 / 剧照 / 章节场景图按作品分组铺平（合集页图床浏览模式数据源）
    /// `GET /collections/{collection_id}/gallery`
    func collectionGallery(collectionId: Int, limit: Int? = nil, offset: Int? = nil, sort: String? = nil, order: String? = nil) async throws -> [API.LibraryGalleryGroupView] {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let offset { query.append(URLQueryItem(name: "offset", value: "\(offset)")) }
        if let sort { query.append(URLQueryItem(name: "sort", value: "\(sort)")) }
        if let order { query.append(URLQueryItem(name: "order", value: "\(order)")) }
        return try await send("GET", "/collections/\(collectionId)/gallery", query: query)
    }

    /// 合集成员（与单库海报墙同一份聚合）
    /// `GET /collections/{collection_id}/items`
    func collectionItemsList(collectionId: Int, limit: Int? = nil, offset: Int? = nil, sort: String? = nil, order: String? = nil) async throws -> [API.LibraryItemView] {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let offset { query.append(URLQueryItem(name: "offset", value: "\(offset)")) }
        if let sort { query.append(URLQueryItem(name: "sort", value: "\(sort)")) }
        if let order { query.append(URLQueryItem(name: "order", value: "\(order)")) }
        return try await send("GET", "/collections/\(collectionId)/items", query: query)
    }

    /// 把作品加进手动合集（已在里面的忽略，不报错）
    /// `POST /collections/{collection_id}/items`
    func collectionItemsAdd(collectionId: Int, body: API.CollectionItemsPayload) async throws -> API.CollectionView {
        return try await send("POST", "/collections/\(collectionId)/items", body: body)
    }

    /// 把一部作品移出手动合集（不动作品本身）
    /// `DELETE /collections/{collection_id}/items/{media_item_id}`
    func collectionItemsRemove(collectionId: Int, mediaItemId: Int) async throws -> API.CollectionView {
        return try await send("DELETE", "/collections/\(collectionId)/items/\(mediaItemId)")
    }

    /// 手动合集的排序（拖拽结果整体覆盖）
    /// `PUT /collections/{collection_id}/order`
    func collectionItemsReorder(collectionId: Int, body: API.CollectionItemsPayload) async throws -> API.CollectionView {
        return try await send("PUT", "/collections/\(collectionId)/order", body: body)
    }

    /// 系列合集的「已有 N / 共 M」与缺片名单
    /// `GET /collections/{collection_id}/series`
    func collectionSeriesGet(collectionId: Int) async throws -> API.CollectionSeriesView {
        return try await send("GET", "/collections/\(collectionId)/series")
    }

    /// 取消这个合集的分享，链接立刻失效
    /// `DELETE /collections/{collection_id}/share`
    func collectionShareRevoke(collectionId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/collections/\(collectionId)/share")
    }

    /// 看这个合集当前的分享链接、密码和有效期
    /// `GET /collections/{collection_id}/share`
    func collectionShareGet(collectionId: Int) async throws -> API.ShareView? {
        return try await send("GET", "/collections/\(collectionId)/share")
    }

    /// 把整个合集分享出去，拿到链接的人不用登录就能看
    /// `POST /collections/{collection_id}/share`
    func collectionShareCreate(collectionId: Int, body: API.ShareCreateRequest) async throws -> API.ShareView {
        return try await send("POST", "/collections/\(collectionId)/share", body: body)
    }

    /// 列出指定来源中可浏览的电影或剧集片单
    /// `GET /discover/collections`
    func discoverListCollections(mediaType: API.MediaKind, provider: API.MediaSource) async throws -> API.DiscoveryCollectionListView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "media_type", value: "\(mediaType)"))
        query.append(URLQueryItem(name: "provider", value: "\(provider)"))
        return try await send("GET", "/discover/collections", query: query)
    }

    /// 浏览指定片单中的电影或剧集
    /// `GET /discover/collections/{collection_ref}/titles`
    func discoverBrowseCollection(collectionRef: String, limit: Int? = nil, page: Int? = nil) async throws -> API.DiscoveryCollectionTitlesView {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let page { query.append(URLQueryItem(name: "page", value: "\(page)")) }
        return try await send("GET", "/discover/collections/\(collectionRef)/titles", query: query)
    }

    /// 获取组合发现可用的类型选项
    /// `GET /discover/filters`
    func discoverFilterOptions(mediaType: API.MediaKind) async throws -> API.DiscoveryFilterOptionsView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "media_type", value: "\(mediaType)"))
        return try await send("GET", "/discover/filters", query: query)
    }

    /// 获取 TMDB 影人的完整影视履历及本地状态
    /// `GET /discover/people/{tmdb_person_id}`
    func discoverGetPersonDetails(tmdbPersonId: Int) async throws -> API.DiscoveredPersonDetailsView {
        return try await send("GET", "/discover/people/\(tmdbPersonId)")
    }

    /// 读取「正在热映/即将上映」的院线地区
    /// `GET /discover/region`
    func discoverRegionShow() async throws -> API.DiscoverRegionView {
        return try await send("GET", "/discover/region")
    }

    /// 切换院线地区（选择即保存，立即生效）
    /// `PUT /discover/region`
    func discoverRegionSet(body: API.DiscoverRegionPayload) async throws -> API.DiscoverRegionView {
        return try await send("PUT", "/discover/region", body: body)
    }

    /// 按类型、地区、年份、评分、片长和排序组合发现影视
    /// `GET /discover/titles`
    func discoverFilterTitles(mediaType: API.MediaKind, genreIds: [Int]? = nil, originCountry: String? = nil, year: Int? = nil, ratingGte: Double? = nil, runtimeLte: Int? = nil, sort: API.DiscoverySort? = nil, page: Int? = nil) async throws -> API.DiscoveryTitlePageView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "media_type", value: "\(mediaType)"))
        for value in genreIds ?? [] { query.append(URLQueryItem(name: "genre_ids", value: "\(value)")) }
        if let originCountry { query.append(URLQueryItem(name: "origin_country", value: "\(originCountry)")) }
        if let year { query.append(URLQueryItem(name: "year", value: "\(year)")) }
        if let ratingGte { query.append(URLQueryItem(name: "rating_gte", value: "\(ratingGte)")) }
        if let runtimeLte { query.append(URLQueryItem(name: "runtime_lte", value: "\(runtimeLte)")) }
        if let sort { query.append(URLQueryItem(name: "sort", value: "\(sort)")) }
        if let page { query.append(URLQueryItem(name: "page", value: "\(page)")) }
        return try await send("GET", "/discover/titles", query: query)
    }

    /// 获取影视条目的完整资料、演职员、图片和相关推荐
    /// `GET /discover/titles/{title_ref}`
    func discoverGetTitleDetails(titleRef: String) async throws -> API.DiscoveredTitleDetailsView {
        return try await send("GET", "/discover/titles/\(titleRef)")
    }

    /// 列出已配置的下载器及连接状态
    /// `GET /downloaders`
    func dlList() async throws -> [API.DownloaderView] {
        return try await send("GET", "/downloaders")
    }

    /// 添加一个下载器（保存后异步测试连接）
    /// `POST /downloaders`
    func dlAdd(body: API.DownloaderPayload) async throws -> API.DownloaderView {
        return try await send("POST", "/downloaders", body: body)
    }

    /// 识别手动搜索结果并预演媒体库与监听导入投递目录
    /// `POST /downloaders/resolve-target`
    func dlResolveTarget(body: API.ManualDownloadTargetPayload) async throws -> API.ManualDownloadTargetView {
        return try await send("POST", "/downloaders/resolve-target", body: body)
    }

    /// 把一条搜索结果种子提交到下载器（缺省默认下载器，可指定分流）
    /// `POST /downloaders/submit`
    func dlSubmit(body: API.DownloadSubmitPayload) async throws -> API.DownloadSubmitView {
        return try await send("POST", "/downloaders/submit", body: body)
    }

    /// 我的保存位置记忆（搜索结果页据此决定弹确认条还是完整弹窗）
    /// `GET /downloaders/target-prefs`
    func dlTargetPrefsList() async throws -> [API.DownloadTargetPrefView] {
        return try await send("GET", "/downloaders/target-prefs")
    }

    /// 不再记住某个分类的保存位置
    /// `DELETE /downloaders/target-prefs/{category}`
    func dlTargetPrefsForget(category: String) async throws -> Void {
        let _: API.JSONValue? = try await send("DELETE", "/downloaders/target-prefs/\(category)")
    }

    /// 列出所有下载器里正在跑的任务，以及它们对应哪部片、哪一集
    /// `GET /downloaders/tasks`
    func dlTasks() async throws -> API.DownloadTaskListView {
        return try await send("GET", "/downloaders/tasks")
    }

    /// 删除下载器配置
    /// `DELETE /downloaders/{downloader_id}`
    func dlDelete(downloaderId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/downloaders/\(downloaderId)")
    }

    /// 获取单个下载器详情
    /// `GET /downloaders/{downloader_id}`
    func dlShow(downloaderId: Int) async throws -> API.DownloaderView {
        return try await send("GET", "/downloaders/\(downloaderId)")
    }

    /// 更新下载器配置（更新后重新测试连接）
    /// `PUT /downloaders/{downloader_id}`
    func dlUpdate(downloaderId: Int, body: API.DownloaderPayload) async throws -> API.DownloaderView {
        return try await send("PUT", "/downloaders/\(downloaderId)", body: body)
    }

    /// 设为默认下载器
    /// `POST /downloaders/{downloader_id}/default`
    func dlDefaultSet(downloaderId: Int) async throws -> API.DownloaderView {
        return try await send("POST", "/downloaders/\(downloaderId)/default")
    }

    /// 读取下载器的全局限速与任务队列上限
    /// `GET /downloaders/{downloader_id}/limits`
    func dlLimits(downloaderId: Int) async throws -> API.DownloaderLimitsView {
        return try await send("GET", "/downloaders/\(downloaderId)/limits")
    }

    /// 设置下载器的全局限速与任务队列上限
    /// `PUT /downloaders/{downloader_id}/limits`
    func dlLimitsSet(downloaderId: Int, body: API.DownloaderLimitsUpdate) async throws -> API.DownloaderLimitsView {
        return try await send("PUT", "/downloaders/\(downloaderId)/limits", body: body)
    }

    /// 启用 / 停用下载器
    /// `PATCH /downloaders/{downloader_id}/status`
    func dlStatusSet(downloaderId: Int, body: API.DownloaderStatusUpdate) async throws -> API.DownloaderView {
        return try await send("PATCH", "/downloaders/\(downloaderId)/status", body: body)
    }

    /// 从下载器里删掉一个种子任务，可选连同已下好的文件一起删
    /// `DELETE /downloaders/{downloader_id}/torrents/{info_hash}`
    func dlTorrentDelete(downloaderId: Int, infoHash: String, deleteFiles: Bool? = nil) async throws -> API.DownloadTaskDeleteView {
        var query: [URLQueryItem] = []
        if let deleteFiles { query.append(URLQueryItem(name: "delete_files", value: "\(deleteFiles)")) }
        return try await send("DELETE", "/downloaders/\(downloaderId)/torrents/\(infoHash)", query: query)
    }

    /// 给卡住不动的下载立刻换一个同品质的种子源
    /// `POST /downloaders/{downloader_id}/torrents/{info_hash}/replace`
    func dlTorrentReplace(downloaderId: Int, infoHash: String) async throws -> API.DownloadTaskReplaceView {
        return try await send("POST", "/downloaders/\(downloaderId)/torrents/\(infoHash)/replace")
    }

    /// 手动重新测试连接
    /// `POST /downloaders/{downloader_id}/verify`
    func dlVerify(downloaderId: Int) async throws -> API.DownloaderView {
        return try await send("POST", "/downloaders/\(downloaderId)/verify")
    }

    /// 推送某站点的 Cookie（保存后异步验证）
    /// `POST /extension/cookies`
    func extensionCookiesPush(body: API.CookiePushRequest) async throws -> API.CookieSyncResult {
        return try await send("POST", "/extension/cookies", body: body)
    }

    /// 连接与令牌自检
    /// `GET /extension/ping`
    func extensionPing() async throws -> API.PingResult {
        return try await send("GET", "/extension/ping")
    }

    /// 列出支持 Cookie 同步的站点及配置状态
    /// `GET /extension/sites`
    func extensionSitesList() async throws -> [API.ExtensionSiteView] {
        return try await send("GET", "/extension/sites")
    }

    /// 关闭同步（撤销令牌）
    /// `DELETE /extension/token`
    func extensionTokenRevoke() async throws -> API.SyncTokenView {
        return try await send("DELETE", "/extension/token")
    }

    /// 查看当前同步令牌
    /// `GET /extension/token`
    func extensionTokenShow() async throws -> API.SyncTokenView {
        return try await send("GET", "/extension/token")
    }

    /// 生成 / 重新生成同步令牌
    /// `POST /extension/token`
    func extensionTokenCreate() async throws -> API.SyncTokenView {
        return try await send("POST", "/extension/token")
    }

    /// 列出服务器上某个目录下有哪些子目录
    /// `GET /fs/browse`
    func fsBrowse(path: String? = nil) async throws -> API.FsBrowseView {
        var query: [URLQueryItem] = []
        if let path { query.append(URLQueryItem(name: "path", value: "\(path)")) }
        return try await send("GET", "/fs/browse", query: query)
    }

    /// Health check
    /// `GET /health`
    func healthCheck() async throws -> API.HealthResponse {
        return try await raw("GET", "/health")
    }

    /// 列出全部监听导入规则
    /// `GET /import-watch`
    func watchList() async throws -> [API.ImportWatchView] {
        return try await send("GET", "/import-watch")
    }

    /// 创建监听导入规则（硬链接策略保存即做同盘检测）
    /// `POST /import-watch`
    func watchCreate(body: API.ImportWatchPayload) async throws -> API.ImportWatchView {
        return try await send("POST", "/import-watch", body: body)
    }

    /// 认领条目：钉到指定 TMDB 身份并恢复后台入库作业
    /// `POST /import-watch/entries/{entry_id}/claim`
    func watchEntriesClaim(entryId: Int, body: API.MovieclawApiApiRoutesImportWatchClaimPayload) async throws -> API.IngestEntryView {
        return try await send("POST", "/import-watch/entries/\(entryId)/claim", body: body)
    }

    /// 忽略条目：永久跳过，不再处理（可恢复）
    /// `POST /import-watch/entries/{entry_id}/ignore`
    func watchEntriesIgnore(entryId: Int) async throws -> API.IngestEntryView {
        return try await send("POST", "/import-watch/entries/\(entryId)/ignore")
    }

    /// 恢复条目：重新进入处理流程
    /// `POST /import-watch/entries/{entry_id}/restore`
    func watchEntriesRestore(entryId: Int) async throws -> API.IngestEntryView {
        return try await send("POST", "/import-watch/entries/\(entryId)/restore")
    }

    /// 删除监听导入规则（不动磁盘，仅停止监听）
    /// `DELETE /import-watch/{rule_id}`
    func watchDelete(ruleId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/import-watch/\(ruleId)")
    }

    /// 更新监听导入规则
    /// `PUT /import-watch/{rule_id}`
    func watchUpdate(ruleId: Int, body: API.ImportWatchPayload) async throws -> API.ImportWatchView {
        return try await send("PUT", "/import-watch/\(ruleId)", body: body)
    }

    /// 一条规则的台账清单（待处理/失败/已忽略/已入库）
    /// `GET /import-watch/{rule_id}/entries`
    func watchEntries(ruleId: Int, status: String? = nil) async throws -> API.IngestEntriesView {
        var query: [URLQueryItem] = []
        if let status { query.append(URLQueryItem(name: "status", value: "\(status)")) }
        return try await send("GET", "/import-watch/\(ruleId)/entries", query: query)
    }

    /// 查询后台任务（可按状态、类型或资源聚合）
    /// `GET /jobs`
    func jobsList(status: String? = nil, jobType: String? = nil, resourceType: String? = nil, resourceId: String? = nil, activeOnly: Bool? = nil, limit: Int? = nil) async throws -> API.JobListView {
        var query: [URLQueryItem] = []
        if let status { query.append(URLQueryItem(name: "status", value: "\(status)")) }
        if let jobType { query.append(URLQueryItem(name: "job_type", value: "\(jobType)")) }
        if let resourceType { query.append(URLQueryItem(name: "resource_type", value: "\(resourceType)")) }
        if let resourceId { query.append(URLQueryItem(name: "resource_id", value: "\(resourceId)")) }
        if let activeOnly { query.append(URLQueryItem(name: "active_only", value: "\(activeOnly)")) }
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        return try await send("GET", "/jobs", query: query)
    }

    /// 忽略当前所有失败任务
    /// `POST /jobs/dismiss-all`
    func jobsDismissAll(body: API.JobDismissAllRequest? = nil) async throws -> API.JobDismissAllView {
        return try await send("POST", "/jobs/dismiss-all", body: body)
    }

    /// 查看后台任务执行器健康状态
    /// `GET /jobs/health`
    func jobsHealth() async throws -> API.JobWorkerHealthView {
        return try await send("GET", "/jobs/health")
    }

    /// 查看后台任务详情
    /// `GET /jobs/{job_id}`
    func jobsShow(jobId: String) async throws -> API.JobView {
        return try await send("GET", "/jobs/\(jobId)")
    }

    /// 取消后台任务（在安全边界生效）
    /// `POST /jobs/{job_id}/cancel`
    func jobsCancel(jobId: String) async throws -> API.JobCancelView {
        return try await send("POST", "/jobs/\(jobId)/cancel")
    }

    /// 忽略失败的后台任务（不再计入需要处理）
    /// `POST /jobs/{job_id}/dismiss`
    func jobsDismiss(jobId: String, body: API.JobDismissRequest? = nil) async throws -> API.JobDismissView {
        return try await send("POST", "/jobs/\(jobId)/dismiss", body: body)
    }

    /// 查看后台任务事件时间线
    /// `GET /jobs/{job_id}/events`
    func jobsEvents(jobId: String, after: Int? = nil, limit: Int? = nil) async throws -> API.JobEventListView {
        var query: [URLQueryItem] = []
        if let after { query.append(URLQueryItem(name: "after", value: "\(after)")) }
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        return try await send("GET", "/jobs/\(jobId)/events", query: query)
    }

    /// 重试失败、已取消或需要处理的后台任务
    /// `POST /jobs/{job_id}/retry`
    func jobsRetry(jobId: String) async throws -> API.JobRetryView {
        return try await send("POST", "/jobs/\(jobId)/retry")
    }

    /// 撤销忽略，任务回到需要处理
    /// `POST /jobs/{job_id}/undismiss`
    func jobsUndismiss(jobId: String) async throws -> API.JobDismissView {
        return try await send("POST", "/jobs/\(jobId)/undismiss")
    }

    /// 等待任务状态变化（CLI 长轮询）
    /// `GET /jobs/{job_id}/wait`
    func jobsWait(jobId: String, afterRevision: Int? = nil, waitSeconds: Double? = nil) async throws -> API.JobWaitView {
        var query: [URLQueryItem] = []
        if let afterRevision { query.append(URLQueryItem(name: "after_revision", value: "\(afterRevision)")) }
        if let waitSeconds { query.append(URLQueryItem(name: "wait_seconds", value: "\(waitSeconds)")) }
        return try await send("GET", "/jobs/\(jobId)/wait", query: query)
    }

    /// 列出媒体库（含库存统计，可按类型过滤；默认只列当前身份可浏览的库）
    /// `GET /libraries`
    func libraryList(kind: String? = nil, scope: String? = nil) async throws -> [API.LibraryView] {
        var query: [URLQueryItem] = []
        if let kind { query.append(URLQueryItem(name: "kind", value: "\(kind)")) }
        if let scope { query.append(URLQueryItem(name: "scope", value: "\(scope)")) }
        return try await send("GET", "/libraries", query: query)
    }

    /// 创建媒体库（该类型首个库自动成为默认，并自动开始首次扫描）
    /// `POST /libraries`
    func libraryCreate(body: API.LibraryPayload) async throws -> API.LibraryView {
        return try await send("POST", "/libraries", body: body)
    }

    /// 重排媒体库展示顺序（决定首页卡片与「最近添加」分区的排列）
    /// `PUT /libraries/display-order`
    func libraryReorder(body: API.LibraryReorderPayload) async throws -> [String: API.JSONValue] {
        return try await send("PUT", "/libraries/display-order", body: body)
    }

    /// 重复文件：扫描状态 + 三档摘要 + 本页条目
    /// `GET /libraries/duplicate-files`
    func libraryDuplicatesList(tier: String? = nil, reviewKind: String? = nil, q: String? = nil, libraryId: Int? = nil, mediaItemId: Int? = nil, limit: Int? = nil, offset: Int? = nil) async throws -> API.DuplicateFilesData {
        var query: [URLQueryItem] = []
        if let tier { query.append(URLQueryItem(name: "tier", value: "\(tier)")) }
        if let reviewKind { query.append(URLQueryItem(name: "review_kind", value: "\(reviewKind)")) }
        if let q { query.append(URLQueryItem(name: "q", value: "\(q)")) }
        if let libraryId { query.append(URLQueryItem(name: "library_id", value: "\(libraryId)")) }
        if let mediaItemId { query.append(URLQueryItem(name: "media_item_id", value: "\(mediaItemId)")) }
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let offset { query.append(URLQueryItem(name: "offset", value: "\(offset)")) }
        return try await send("GET", "/libraries/duplicate-files", query: query)
    }

    /// 一个单元 / 一季的决定：留这个 / 整季留这个版本 / 都留着
    /// `POST /libraries/duplicate-files/resolve`
    func libraryDuplicatesResolve(body: API.DuplicateResolvePayload) async throws -> API.TrashedBatchResultView {
        return try await send("POST", "/libraries/duplicate-files/resolve", body: body)
    }

    /// 一整档 / 一组一起决定：都按建议清理，或都留着
    /// `POST /libraries/duplicate-files/resolve-all`
    func libraryDuplicatesResolveAll(body: API.DuplicateResolveAllPayload) async throws -> API.TrashedBatchResultView {
        return try await send("POST", "/libraries/duplicate-files/resolve-all", body: body)
    }

    /// 开始扫描重复文件（可恢复后台作业，结论落库供页面读取）
    /// `POST /libraries/duplicate-files/scan`
    func libraryDuplicatesScan() async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/duplicate-files/scan")
    }

    /// 删除该文件的一个外挂字幕（含 AI 生成的字幕；内封轨在容器内，不可删）
    /// `DELETE /libraries/files/{file_id}/subtitles`
    func librarySubtitlesDelete(fileId: Int, filename: String) async throws -> API.SubtitleDeleteResultView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "filename", value: "\(filename)"))
        return try await send("DELETE", "/libraries/files/\(fileId)/subtitles", query: query)
    }

    /// AI 字幕生成预检：选源结果与成本估算（发起确认框素材）
    /// `GET /libraries/files/{file_id}/subtitles/generation-preview`
    func librarySubtitlesPreviewGeneration(fileId: Int, targetLanguage: String? = nil, secondaryLanguage: String? = nil, sourceCandidateKey: String? = nil) async throws -> API.GenPreviewView {
        var query: [URLQueryItem] = []
        if let targetLanguage { query.append(URLQueryItem(name: "target_language", value: "\(targetLanguage)")) }
        if let secondaryLanguage { query.append(URLQueryItem(name: "secondary_language", value: "\(secondaryLanguage)")) }
        if let sourceCandidateKey { query.append(URLQueryItem(name: "source_candidate_key", value: "\(sourceCandidateKey)")) }
        return try await send("GET", "/libraries/files/\(fileId)/subtitles/generation-preview", query: query)
    }

    /// 发起 AI 字幕生成（后台执行；同文件单飞）
    /// `POST /libraries/files/{file_id}/subtitles/generations`
    func librarySubtitlesGenerate(fileId: Int, body: API.GenStartPayload) async throws -> API.JobView {
        return try await send("POST", "/libraries/files/\(fileId)/subtitles/generations", body: body)
    }

    /// 预览一条外挂或文本内封字幕的时间轴内容
    /// `GET /libraries/files/{file_id}/subtitles/preview`
    func uiLibraryFilesPreviewSubtitles(fileId: Int, track: String) async throws -> API.SubtitlePreviewView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "track", value: "\(track)"))
        return try await send("GET", "/libraries/files/\(fileId)/subtitles/preview", query: query)
    }

    /// 一键校准外挂字幕时间轴（音轨互相关；strm 退字幕对字幕）
    /// `POST /libraries/files/{file_id}/subtitles/timing-calibration`
    func librarySubtitlesCalibrateTiming(fileId: Int, body: API.CalibratePayload) async throws -> API.CalibrateResultView {
        return try await send("POST", "/libraries/files/\(fileId)/subtitles/timing-calibration", body: body)
    }

    /// 把多个文件一次关联到同一个 Discover 影视条目
    /// `POST /libraries/identification/file-title-assignments`
    func libraryIdentificationAssignFilesToTitle(body: API.ClaimBatchPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/identification/file-title-assignments", body: body)
    }

    /// 标为「非独立作品」：摘掉身份锚并忽略（花絮/预告类，不动磁盘）
    /// `POST /libraries/identification/files/mark-as-extras`
    func libraryIdentificationMarkFilesAsExtras(body: API.DetachPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/identification/files/mark-as-extras", body: body)
    }

    /// 忽略一个待识别文件：以后扫描不再过问（不动磁盘）
    /// `POST /libraries/identification/files/{file_id}/ignore`
    func libraryIdentificationIgnoreFile(fileId: Int) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/identification/files/\(fileId)/ignore")
    }

    /// 把一个文件明确关联到 Discover 影视条目
    /// `POST /libraries/identification/files/{file_id}/title-assignment`
    func libraryIdentificationAssignFileToTitle(fileId: Int, body: API.MovieclawApiSchemasLibraryClaimPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/identification/files/\(fileId)/title-assignment", body: body)
    }

    /// 恢复已忽略的文件：重新参与识别
    /// `POST /libraries/identification/ignored-file-restorations`
    func libraryIdentificationRestoreFiles(body: API.RestorePayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/identification/ignored-file-restorations", body: body)
    }

    /// 已忽略清单（用户说过「别再问」的文件，可恢复）
    /// `GET /libraries/identification/ignored-files`
    func libraryIdentificationListIgnoredFiles(libraryId: Int? = nil) async throws -> [API.UnidentifiedGroupView] {
        var query: [URLQueryItem] = []
        if let libraryId { query.append(URLQueryItem(name: "library_id", value: "\(libraryId)")) }
        return try await send("GET", "/libraries/identification/ignored-files", query: query)
    }

    /// 身份复核清单（识别器升级后的新旧结论分歧，可按库过滤）
    /// `GET /libraries/identification/review-cases`
    func libraryIdentificationListReviewCases(libraryId: Int? = nil) async throws -> [API.ReviewGroupView] {
        var query: [URLQueryItem] = []
        if let libraryId { query.append(URLQueryItem(name: "library_id", value: "\(libraryId)")) }
        return try await send("GET", "/libraries/identification/review-cases", query: query)
    }

    /// 决定身份复核结果：采纳建议或维持当前身份
    /// `POST /libraries/identification/review-decisions`
    func libraryIdentificationResolveReview(body: API.ReviewResolvePayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/identification/review-decisions", body: body)
    }

    /// 忽略一个媒体库内的全部待识别文件（可恢复，不动磁盘）
    /// `POST /libraries/identification/unidentified-file-ignores`
    func libraryIdentificationIgnoreAllUnidentifiedFiles(body: API.UnidentifiedClearPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/identification/unidentified-file-ignores", body: body)
    }

    /// 待识别清单（按条目目录分组，不含已忽略，可按库过滤）
    /// `GET /libraries/identification/unidentified-files`
    func libraryIdentificationListUnidentifiedFiles(libraryId: Int? = nil) async throws -> [API.UnidentifiedGroupView] {
        var query: [URLQueryItem] = []
        if let libraryId { query.append(URLQueryItem(name: "library_id", value: "\(libraryId)")) }
        return try await send("GET", "/libraries/identification/unidentified-files", query: query)
    }

    /// 整季人工标注片源：把「无法确认」的洗版单元变为可判定
    /// `POST /libraries/media-source-annotations`
    func libraryItemsAnnotateMediaSource(body: API.MediaSourceAnnotationPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/media-source-annotations", body: body)
    }

    /// 整季片源标注的预览：列出将被标注的文件（片源未知或既有人工标注）
    /// `GET /libraries/media-source-annotations/candidates`
    func libraryItemsListMediaSourceAnnotationCandidates(mediaItemId: Int, seasonNumber: Int) async throws -> [API.MediaSourceAnnotationCandidateView] {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "media_item_id", value: "\(mediaItemId)"))
        query.append(URLQueryItem(name: "season_number", value: "\(seasonNumber)"))
        return try await send("GET", "/libraries/media-source-annotations/candidates", query: query)
    }

    /// 清理缺失记录（只删台账，绝不动磁盘）；不带 media_item_id 清整库
    /// `POST /libraries/missing-record-clearances`
    func libraryMissingClearRecords(body: API.MissingClearPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/missing-record-clearances", body: body)
    }

    /// 重新下载缺失内容：缺失单元交回订阅管线（无订阅则按缺失季创建）
    /// `POST /libraries/missing-redownloads`
    func libraryMissingRedownload(body: API.RedownloadPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/missing-redownloads", body: body)
    }

    /// 列出媒体库路由规则可用的媒体类型、地区预设和类型标签
    /// `GET /libraries/routing-options`
    func libraryListRoutingOptions() async throws -> [String: API.JSONValue] {
        return try await send("GET", "/libraries/routing-options")
    }

    /// 回收站：全部待回收文件，按条目分组分页（含摘要与分面计数）
    /// `GET /libraries/trashed-files`
    func libraryRecycleList(q: String? = nil, libraryId: Int? = nil, reason: String? = nil, limit: Int? = nil, offset: Int? = nil) async throws -> API.TrashedFilesData {
        var query: [URLQueryItem] = []
        if let q { query.append(URLQueryItem(name: "q", value: "\(q)")) }
        if let libraryId { query.append(URLQueryItem(name: "library_id", value: "\(libraryId)")) }
        if let reason { query.append(URLQueryItem(name: "reason", value: "\(reason)")) }
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let offset { query.append(URLQueryItem(name: "offset", value: "\(offset)")) }
        return try await send("GET", "/libraries/trashed-files", query: query)
    }

    /// 批量立即清理待回收文件（按 id 或按筛选；真删磁盘）
    /// `POST /libraries/trashed-files/purge`
    func libraryRecyclePurge(body: API.TrashedPurgePayload) async throws -> API.TrashedBatchResultView {
        return try await send("POST", "/libraries/trashed-files/purge", body: body)
    }

    /// 批量恢复待回收文件为在位版本
    /// `POST /libraries/trashed-files/restore`
    func libraryRecycleRestore(body: API.TrashedRestorePayload) async throws -> API.TrashedBatchResultView {
        return try await send("POST", "/libraries/trashed-files/restore", body: body)
    }

    /// 删除媒体库（不动磁盘文件；其订阅回落到该类型默认库；扫描/整理中锁定）
    /// `DELETE /libraries/{library_id}`
    func libraryDelete(libraryId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/libraries/\(libraryId)")
    }

    /// 获取单个媒体库详情
    /// `GET /libraries/{library_id}`
    func libraryGet(libraryId: Int) async throws -> API.LibraryView {
        return try await send("GET", "/libraries/\(libraryId)")
    }

    /// 更新媒体库（类型创建后不可改；变更根路径时要求库空闲）
    /// `PUT /libraries/{library_id}`
    func libraryUpdate(libraryId: Int, body: API.LibraryPayload) async throws -> API.LibraryView {
        return try await send("PUT", "/libraries/\(libraryId)", body: body)
    }

    /// 生成整库的章节（可恢复后台作业；force=true 已有的也重新生成）
    /// `POST /libraries/{library_id}/chapter-images`
    func libraryChapterImagesGenerate(libraryId: Int, force: Bool? = nil) async throws -> [String: API.JSONValue] {
        var query: [URLQueryItem] = []
        if let force { query.append(URLQueryItem(name: "force", value: "\(force)")) }
        return try await send("POST", "/libraries/\(libraryId)/chapter-images", query: query)
    }

    /// 删除媒体库自定义封面（回落到自动拼贴）
    /// `DELETE /libraries/{library_id}/cover`
    func libraryCoverClear(libraryId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/libraries/\(libraryId)/cover")
    }

    /// 设为该类型的默认库
    /// `POST /libraries/{library_id}/default-selection`
    func librarySetDefault(libraryId: Int) async throws -> API.LibraryView {
        return try await send("POST", "/libraries/\(libraryId)/default-selection")
    }

    /// 筛选面板的候选值与计数（每一维排除自身条件后算）
    /// `GET /libraries/{library_id}/facets`
    func libraryItemsFacets(libraryId: Int, tier: String? = nil, g: String? = nil, c: String? = nil, d: String? = nil, w: String? = nil, ratingGte: Double? = nil, rt: String? = nil, lang: String? = nil, res: String? = nil, hdr: Bool? = nil, stock: String? = nil, seriesKeys: String? = nil) async throws -> API.LibraryFacetsView {
        var query: [URLQueryItem] = []
        if let tier { query.append(URLQueryItem(name: "tier", value: "\(tier)")) }
        if let g { query.append(URLQueryItem(name: "g", value: "\(g)")) }
        if let c { query.append(URLQueryItem(name: "c", value: "\(c)")) }
        if let d { query.append(URLQueryItem(name: "d", value: "\(d)")) }
        if let w { query.append(URLQueryItem(name: "w", value: "\(w)")) }
        if let ratingGte { query.append(URLQueryItem(name: "rating_gte", value: "\(ratingGte)")) }
        if let rt { query.append(URLQueryItem(name: "rt", value: "\(rt)")) }
        if let lang { query.append(URLQueryItem(name: "lang", value: "\(lang)")) }
        if let res { query.append(URLQueryItem(name: "res", value: "\(res)")) }
        if let hdr { query.append(URLQueryItem(name: "hdr", value: "\(hdr)")) }
        if let stock { query.append(URLQueryItem(name: "stock", value: "\(stock)")) }
        if let seriesKeys { query.append(URLQueryItem(name: "series_keys", value: "\(seriesKeys)")) }
        return try await send("GET", "/libraries/\(libraryId)/facets", query: query)
    }

    /// 预览整理计划：每个文件改成什么名、哪些跳过及原因（只读，不动磁盘）
    /// `POST /libraries/{library_id}/file-organization-preview`
    func workflowLibraryOrganizeFilesPreview(libraryId: Int) async throws -> API.OrganizePreviewView {
        return try await send("POST", "/libraries/\(libraryId)/file-organization-preview")
    }

    /// 开始整理：按规范命名批量改名归位（可恢复后台作业）
    /// `POST /libraries/{library_id}/file-organizations`
    func workflowLibraryOrganizeFilesStart(libraryId: Int) async throws -> API.OrganizeStartView {
        return try await send("POST", "/libraries/\(libraryId)/file-organizations")
    }

    /// 库内条目的图廊：海报 / 剧照 / 章节场景图按条目分组铺平（图床浏览模式数据源）
    /// `GET /libraries/{library_id}/gallery`
    func uiLibraryGallery(libraryId: Int, limit: Int? = nil, offset: Int? = nil, sort: String? = nil, order: String? = nil, g: String? = nil, c: String? = nil, d: String? = nil, w: String? = nil, ratingGte: Double? = nil, rt: String? = nil, lang: String? = nil, res: String? = nil, hdr: Bool? = nil, stock: String? = nil, seriesKeys: String? = nil) async throws -> [API.LibraryGalleryGroupView] {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let offset { query.append(URLQueryItem(name: "offset", value: "\(offset)")) }
        if let sort { query.append(URLQueryItem(name: "sort", value: "\(sort)")) }
        if let order { query.append(URLQueryItem(name: "order", value: "\(order)")) }
        if let g { query.append(URLQueryItem(name: "g", value: "\(g)")) }
        if let c { query.append(URLQueryItem(name: "c", value: "\(c)")) }
        if let d { query.append(URLQueryItem(name: "d", value: "\(d)")) }
        if let w { query.append(URLQueryItem(name: "w", value: "\(w)")) }
        if let ratingGte { query.append(URLQueryItem(name: "rating_gte", value: "\(ratingGte)")) }
        if let rt { query.append(URLQueryItem(name: "rt", value: "\(rt)")) }
        if let lang { query.append(URLQueryItem(name: "lang", value: "\(lang)")) }
        if let res { query.append(URLQueryItem(name: "res", value: "\(res)")) }
        if let hdr { query.append(URLQueryItem(name: "hdr", value: "\(hdr)")) }
        if let stock { query.append(URLQueryItem(name: "stock", value: "\(stock)")) }
        if let seriesKeys { query.append(URLQueryItem(name: "series_keys", value: "\(seriesKeys)")) }
        return try await send("GET", "/libraries/\(libraryId)/gallery", query: query)
    }

    /// 库内条目 id 集合（前端判定「已入库」用）
    /// `GET /libraries/{library_id}/item-ids`
    func uiLibraryItemsIds(libraryId: Int) async throws -> [Int] {
        return try await send("GET", "/libraries/\(libraryId)/item-ids")
    }

    /// 海报墙的跳转索引（按标题：A-Z 首字母档；按内容时间：月份档）
    /// `GET /libraries/{library_id}/item-index`
    func uiLibraryItemsIndex(libraryId: Int, sort: String? = nil, order: String? = nil, g: String? = nil, c: String? = nil, d: String? = nil, w: String? = nil, ratingGte: Double? = nil, rt: String? = nil, lang: String? = nil, res: String? = nil, hdr: Bool? = nil, stock: String? = nil, seriesKeys: String? = nil) async throws -> [API.LibraryIndexEntryView] {
        var query: [URLQueryItem] = []
        if let sort { query.append(URLQueryItem(name: "sort", value: "\(sort)")) }
        if let order { query.append(URLQueryItem(name: "order", value: "\(order)")) }
        if let g { query.append(URLQueryItem(name: "g", value: "\(g)")) }
        if let c { query.append(URLQueryItem(name: "c", value: "\(c)")) }
        if let d { query.append(URLQueryItem(name: "d", value: "\(d)")) }
        if let w { query.append(URLQueryItem(name: "w", value: "\(w)")) }
        if let ratingGte { query.append(URLQueryItem(name: "rating_gte", value: "\(ratingGte)")) }
        if let rt { query.append(URLQueryItem(name: "rt", value: "\(rt)")) }
        if let lang { query.append(URLQueryItem(name: "lang", value: "\(lang)")) }
        if let res { query.append(URLQueryItem(name: "res", value: "\(res)")) }
        if let hdr { query.append(URLQueryItem(name: "hdr", value: "\(hdr)")) }
        if let stock { query.append(URLQueryItem(name: "stock", value: "\(stock)")) }
        if let seriesKeys { query.append(URLQueryItem(name: "series_keys", value: "\(seriesKeys)")) }
        return try await send("GET", "/libraries/\(libraryId)/item-index", query: query)
    }

    /// 预检批量转移：空间、冲突、硬链接与做种影响一次算清（只读，不动磁盘）
    /// `POST /libraries/{library_id}/item-transfer-preview`
    func workflowLibraryTransferItemsPreview(libraryId: Int, body: API.BatchTransferPayload) async throws -> API.BatchTransferPreviewView {
        return try await send("POST", "/libraries/\(libraryId)/item-transfer-preview", body: body)
    }

    /// 条目转移的实时进度与最近一次结论
    /// `GET /libraries/{library_id}/item-transfer-status`
    func libraryItemsGetTransferStatus(libraryId: Int) async throws -> API.TransferStatusView {
        return try await send("GET", "/libraries/\(libraryId)/item-transfer-status")
    }

    /// 批量转移条目到另一个媒体库：一次提交，后台逐条搬运并随迁台账
    /// `POST /libraries/{library_id}/item-transfers`
    func workflowLibraryTransferItemsStart(libraryId: Int, body: API.BatchTransferPayload) async throws -> API.TransferStartView {
        return try await send("POST", "/libraries/\(libraryId)/item-transfers", body: body)
    }

    /// 库内媒体条目的库存聚合（单库海报墙数据源）
    /// `GET /libraries/{library_id}/items`
    func libraryItemsList(libraryId: Int, sort: String? = nil, order: String? = nil, limit: Int? = nil, offset: Int? = nil, identity: String? = nil, g: String? = nil, c: String? = nil, d: String? = nil, w: String? = nil, ratingGte: Double? = nil, rt: String? = nil, lang: String? = nil, res: String? = nil, hdr: Bool? = nil, stock: String? = nil, seriesKeys: String? = nil) async throws -> [API.LibraryItemView] {
        var query: [URLQueryItem] = []
        if let sort { query.append(URLQueryItem(name: "sort", value: "\(sort)")) }
        if let order { query.append(URLQueryItem(name: "order", value: "\(order)")) }
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let offset { query.append(URLQueryItem(name: "offset", value: "\(offset)")) }
        if let identity { query.append(URLQueryItem(name: "identity", value: "\(identity)")) }
        if let g { query.append(URLQueryItem(name: "g", value: "\(g)")) }
        if let c { query.append(URLQueryItem(name: "c", value: "\(c)")) }
        if let d { query.append(URLQueryItem(name: "d", value: "\(d)")) }
        if let w { query.append(URLQueryItem(name: "w", value: "\(w)")) }
        if let ratingGte { query.append(URLQueryItem(name: "rating_gte", value: "\(ratingGte)")) }
        if let rt { query.append(URLQueryItem(name: "rt", value: "\(rt)")) }
        if let lang { query.append(URLQueryItem(name: "lang", value: "\(lang)")) }
        if let res { query.append(URLQueryItem(name: "res", value: "\(res)")) }
        if let hdr { query.append(URLQueryItem(name: "hdr", value: "\(hdr)")) }
        if let stock { query.append(URLQueryItem(name: "stock", value: "\(stock)")) }
        if let seriesKeys { query.append(URLQueryItem(name: "series_keys", value: "\(seriesKeys)")) }
        return try await send("GET", "/libraries/\(libraryId)/items", query: query)
    }

    /// 从磁盘彻底删除条目（整个刮削目录：视频+NFO+海报+字幕一起清除）
    /// `DELETE /libraries/{library_id}/items/{media_item_id}`
    func libraryItemsDelete(libraryId: Int, mediaItemId: Int) async throws -> API.ItemDeleteResultView {
        return try await send("DELETE", "/libraries/\(libraryId)/items/\(mediaItemId)")
    }

    /// 条目详情：基本信息 + NFO 本地刮削元数据 + 逐文件真实介质规格
    /// `GET /libraries/{library_id}/items/{media_item_id}`
    func libraryItemsGet(libraryId: Int, mediaItemId: Int) async throws -> API.LibraryItemDetailView {
        return try await send("GET", "/libraries/\(libraryId)/items/\(mediaItemId)")
    }

    /// 条目的候选海报/背景图列表（选图前先看这里）
    /// `GET /libraries/{library_id}/items/{media_item_id}/artwork/candidates`
    func libraryArtworkListCandidates(libraryId: Int, mediaItemId: Int) async throws -> API.ArtworkCandidatesView {
        return try await send("GET", "/libraries/\(libraryId)/items/\(mediaItemId)/artwork/candidates")
    }

    /// 选定海报/背景（当场落盘并覆盖媒体目录；此后刷新不再覆盖）
    /// `POST /libraries/{library_id}/items/{media_item_id}/artwork/select`
    func libraryArtworkSelect(libraryId: Int, mediaItemId: Int, body: API.ArtworkSelectPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/\(libraryId)/items/\(mediaItemId)/artwork/select", body: body)
    }

    /// 重新生成单个条目的章节（全部重抓，可恢复后台作业）
    /// `POST /libraries/{library_id}/items/{media_item_id}/chapter-images`
    func libraryItemsRegenerateChapterImages(libraryId: Int, mediaItemId: Int) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/\(libraryId)/items/\(mediaItemId)/chapter-images")
    }

    /// 剧集条目一季的分集清单（集名/简介/剧照 + 拥有状态，分集横滚区数据源）
    /// `GET /libraries/{library_id}/items/{media_item_id}/episodes`
    func libraryItemsListEpisodes(libraryId: Int, mediaItemId: Int, seasonNumber: Int) async throws -> API.SeasonEpisodesView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "season_number", value: "\(seasonNumber)"))
        return try await send("GET", "/libraries/\(libraryId)/items/\(mediaItemId)/episodes", query: query)
    }

    /// 从磁盘删除条目的单个文件（含同名 NFO/字幕/图片附属文件）
    /// `DELETE /libraries/{library_id}/items/{media_item_id}/files/{file_id}`
    func libraryItemsDeleteFile(libraryId: Int, mediaItemId: Int, fileId: Int) async throws -> API.ItemDeleteResultView {
        return try await send("DELETE", "/libraries/\(libraryId)/items/\(mediaItemId)/files/\(fileId)")
    }

    /// 立即清理一个待回收的文件（真删磁盘）
    /// `POST /libraries/{library_id}/items/{media_item_id}/files/{file_id}/purge`
    func libraryItemsPurgeFile(libraryId: Int, mediaItemId: Int, fileId: Int) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/\(libraryId)/items/\(mediaItemId)/files/\(fileId)/purge")
    }

    /// 把待回收的文件恢复为在位版本
    /// `POST /libraries/{library_id}/items/{media_item_id}/files/{file_id}/restore`
    func libraryItemsRestoreFile(libraryId: Int, mediaItemId: Int, fileId: Int) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/\(libraryId)/items/\(mediaItemId)/files/\(fileId)/restore")
    }

    /// 刷新单个条目的元数据（强制重刮 TMDB，可恢复后台作业）
    /// `POST /libraries/{library_id}/items/{media_item_id}/metadata/refresh`
    func libraryItemsRefreshMetadata(libraryId: Int, mediaItemId: Int) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/\(libraryId)/items/\(mediaItemId)/metadata/refresh")
    }

    /// 修正识别结果（预览）：重走识别链只出结论，不改台账
    /// `POST /libraries/{library_id}/items/{media_item_id}/reidentification-preview`
    func libraryItemsPreviewReidentification(libraryId: Int, mediaItemId: Int) async throws -> API.ReidentifyPreviewView {
        return try await send("POST", "/libraries/\(libraryId)/items/\(mediaItemId)/reidentification-preview")
    }

    /// 重新识别条目：全部在位文件重走识别链（NFO → 名称解析 → TMDB 收敛）
    /// `POST /libraries/{library_id}/items/{media_item_id}/reidentifications`
    func libraryItemsReidentify(libraryId: Int, mediaItemId: Int) async throws -> API.ReidentifyResultView {
        return try await send("POST", "/libraries/\(libraryId)/items/\(mediaItemId)/reidentifications")
    }

    /// 取消这部影片的分享，链接立刻失效
    /// `DELETE /libraries/{library_id}/items/{media_item_id}/share`
    func libraryItemsShareRevoke(libraryId: Int, mediaItemId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/libraries/\(libraryId)/items/\(mediaItemId)/share")
    }

    /// 看这部影片当前的分享链接、密码和有效期
    /// `GET /libraries/{library_id}/items/{media_item_id}/share`
    func libraryItemsShareGet(libraryId: Int, mediaItemId: Int) async throws -> API.ShareView? {
        return try await send("GET", "/libraries/\(libraryId)/items/\(mediaItemId)/share")
    }

    /// 生成一条观看链接，发给没有账号的人也能看
    /// `POST /libraries/{library_id}/items/{media_item_id}/share`
    func libraryItemsShareCreate(libraryId: Int, mediaItemId: Int, body: API.ShareCreateRequest) async throws -> API.ShareView {
        return try await send("POST", "/libraries/\(libraryId)/items/\(mediaItemId)/share", body: body)
    }

    /// 预览条目转移：哪些目录/文件搬到目标库的什么位置（只读，不动磁盘）
    /// `GET /libraries/{library_id}/items/{media_item_id}/transfer-preview`
    func libraryItemsPreviewTransfer(libraryId: Int, mediaItemId: Int, targetLibraryId: Int) async throws -> API.TransferPreviewView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "target_library_id", value: "\(targetLibraryId)"))
        return try await send("GET", "/libraries/\(libraryId)/items/\(mediaItemId)/transfer-preview", query: query)
    }

    /// 转移条目到另一个媒体库：整个条目目录搬到目标库主根，台账随迁
    /// `POST /libraries/{library_id}/items/{media_item_id}/transfers`
    func libraryItemsTransfer(libraryId: Int, mediaItemId: Int, body: API.TransferPayload) async throws -> API.TransferStartView {
        return try await send("POST", "/libraries/\(libraryId)/items/\(mediaItemId)/transfers", body: body)
    }

    /// 整库刷新元数据：全部已识别条目重新刮削（可恢复后台作业）
    /// `POST /libraries/{library_id}/metadata/refresh`
    func libraryMetadataRefreshLibrary(libraryId: Int) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/\(libraryId)/metadata/refresh")
    }

    /// 整库元数据刷新的实时状态（进度 + 正在处理哪几部、各在什么阶段）
    /// `GET /libraries/{library_id}/metadata/refresh/progress`
    func libraryMetadataGetRefreshStatus(libraryId: Int) async throws -> API.MetadataRefreshView {
        return try await send("GET", "/libraries/\(libraryId)/metadata/refresh/progress")
    }

    /// 停止进行中的整库元数据刷新（已刷完的保留）
    /// `POST /libraries/{library_id}/metadata/refresh/stop`
    func libraryMetadataStopRefresh(libraryId: Int) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/\(libraryId)/metadata/refresh/stop")
    }

    /// 缺失清单：文件已不在磁盘的库存，按条目聚合
    /// `GET /libraries/{library_id}/missing`
    func libraryMissingList(libraryId: Int) async throws -> [API.MissingItemView] {
        return try await send("GET", "/libraries/\(libraryId)/missing")
    }

    /// 预览历史根路径迁移修复（只读，不扫描、不修改台账）
    /// `POST /libraries/{library_id}/path-reconciliation-preview`
    func workflowLibraryReconcilePathsPreview(libraryId: Int, body: API.PathReconcilePayload) async throws -> API.PathReconcilePreviewView {
        return try await send("POST", "/libraries/\(libraryId)/path-reconciliation-preview", body: body)
    }

    /// 执行历史根路径迁移修复（重新扫描新根并收口旧路径台账）
    /// `POST /libraries/{library_id}/path-reconciliations`
    func workflowLibraryReconcilePathsStart(libraryId: Int, body: API.PathReconcilePayload) async throws -> API.ScanResultView {
        return try await send("POST", "/libraries/\(libraryId)/path-reconciliations", body: body)
    }

    /// 筛空时的放宽建议（只列救得回内容的条件）
    /// `GET /libraries/{library_id}/relax`
    func libraryItemsRelax(libraryId: Int, g: String? = nil, c: String? = nil, d: String? = nil, w: String? = nil, ratingGte: Double? = nil, rt: String? = nil, lang: String? = nil, res: String? = nil, hdr: Bool? = nil, stock: String? = nil, seriesKeys: String? = nil) async throws -> API.LibraryRelaxView {
        var query: [URLQueryItem] = []
        if let g { query.append(URLQueryItem(name: "g", value: "\(g)")) }
        if let c { query.append(URLQueryItem(name: "c", value: "\(c)")) }
        if let d { query.append(URLQueryItem(name: "d", value: "\(d)")) }
        if let w { query.append(URLQueryItem(name: "w", value: "\(w)")) }
        if let ratingGte { query.append(URLQueryItem(name: "rating_gte", value: "\(ratingGte)")) }
        if let rt { query.append(URLQueryItem(name: "rt", value: "\(rt)")) }
        if let lang { query.append(URLQueryItem(name: "lang", value: "\(lang)")) }
        if let res { query.append(URLQueryItem(name: "res", value: "\(res)")) }
        if let hdr { query.append(URLQueryItem(name: "hdr", value: "\(hdr)")) }
        if let stock { query.append(URLQueryItem(name: "stock", value: "\(stock)")) }
        if let seriesKeys { query.append(URLQueryItem(name: "series_keys", value: "\(seriesKeys)")) }
        return try await send("GET", "/libraries/\(libraryId)/relax", query: query)
    }

    /// 预检根路径归并：条目会搬到哪、空间够不够、根配置怎么变（只读）
    /// `POST /libraries/{library_id}/root-consolidation-preview`
    func workflowLibraryConsolidateRootsPreview(libraryId: Int, body: API.ConsolidateRootsPayload) async throws -> API.ConsolidateRootsPreviewView {
        return try await send("POST", "/libraries/\(libraryId)/root-consolidation-preview", body: body)
    }

    /// 归并根路径：把若干个根下的条目搬到一个根，台账随迁、根配置收口
    /// `POST /libraries/{library_id}/root-consolidations`
    func workflowLibraryConsolidateRootsStart(libraryId: Int, body: API.ConsolidateRootsPayload) async throws -> API.TransferStartView {
        return try await send("POST", "/libraries/\(libraryId)/root-consolidations", body: body)
    }

    /// 扫描该库的根路径，把存量文件识别入账（后台执行）
    /// `POST /libraries/{library_id}/scan`
    func libraryScanStart(libraryId: Int) async throws -> API.ScanResultView {
        return try await send("POST", "/libraries/\(libraryId)/scan")
    }

    /// 停止进行中的扫描（已入账的保留，剩余文件下次扫描继续）
    /// `POST /libraries/{library_id}/scan/stop`
    func libraryScanStop(libraryId: Int) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/libraries/\(libraryId)/scan/stop")
    }

    /// 查看 AI 设定（各用途的默认模型）
    /// `GET /llm/defaults`
    func llmDefaultsShow() async throws -> API.LlmDefaultsView {
        return try await send("GET", "/llm/defaults")
    }

    /// 保存 AI 设定（各用途的默认模型）
    /// `PUT /llm/defaults`
    func llmDefaultsUpdate(body: API.LlmDefaultsPayload) async throws -> API.LlmDefaultsView {
        return try await send("PUT", "/llm/defaults", body: body)
    }

    /// 列出对话框可选的全部模型（跨实例）
    /// `GET /llm/models`
    func llmModels() async throws -> [API.LlmModelOptionView] {
        return try await send("GET", "/llm/models")
    }

    /// 列出可接入的供应商类型及其模型目录
    /// `GET /llm/presets`
    func llmPresets() async throws -> [API.LlmPresetView] {
        return try await send("GET", "/llm/presets")
    }

    /// 列出已接入的模型供应商实例
    /// `GET /llm/providers`
    func llmProvidersList() async throws -> [API.LlmProviderView] {
        return try await send("GET", "/llm/providers")
    }

    /// 接入一个模型供应商实例（保存后异步测试连接）
    /// `POST /llm/providers`
    func llmProvidersCreate(body: API.LlmProviderPayload) async throws -> API.LlmProviderView {
        return try await send("POST", "/llm/providers", body: body)
    }

    /// 删除一个模型供应商实例
    /// `DELETE /llm/providers/{provider_id}`
    func llmProvidersDelete(providerId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/llm/providers/\(providerId)")
    }

    /// 查看一个模型供应商实例
    /// `GET /llm/providers/{provider_id}`
    func llmProvidersShow(providerId: Int) async throws -> API.LlmProviderView {
        return try await send("GET", "/llm/providers/\(providerId)")
    }

    /// 修改一个模型供应商实例（保存后异步测试连接）
    /// `PUT /llm/providers/{provider_id}`
    func llmProvidersUpdate(providerId: Int, body: API.LlmProviderPayload) async throws -> API.LlmProviderView {
        return try await send("PUT", "/llm/providers/\(providerId)", body: body)
    }

    /// 手动重新测试一个实例的模型连接
    /// `POST /llm/providers/{provider_id}/verify`
    func llmProvidersVerify(providerId: Int) async throws -> API.LlmProviderView {
        return try await send("POST", "/llm/providers/\(providerId)/verify")
    }

    /// 新建 MCP 端点（令牌明文仅本次返回）
    /// `POST /mcp/endpoints`
    func mcpEndpointsCreate(body: API.EndpointCreateRequest) async throws -> API.EndpointCreatedView {
        return try await send("POST", "/mcp/endpoints", body: body)
    }

    /// 试算：给定服务集合会暴露哪些工具、占多少上下文
    /// `POST /mcp/endpoints/preview`
    func mcpEndpointsPreview(body: API.PreviewRequest) async throws -> API.PreviewView {
        return try await send("POST", "/mcp/endpoints/preview", body: body)
    }

    /// 删除端点（地址与令牌一并作废）
    /// `DELETE /mcp/endpoints/{endpoint_id}`
    func mcpEndpointsDelete(endpointId: String) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/mcp/endpoints/\(endpointId)")
    }

    /// 修改端点的名称、服务、工具模式或启停状态
    /// `PUT /mcp/endpoints/{endpoint_id}`
    func mcpEndpointsUpdate(endpointId: String, body: API.EndpointUpdateRequest) async throws -> API.EndpointView {
        return try await send("PUT", "/mcp/endpoints/\(endpointId)", body: body)
    }

    /// 自检端点：协议握手、工具面与一次只读调用
    /// `POST /mcp/endpoints/{endpoint_id}/check`
    func mcpEndpointsCheck(endpointId: String) async throws -> API.SelfCheckView {
        return try await send("POST", "/mcp/endpoints/\(endpointId)/check")
    }

    /// 轮换端点令牌（旧令牌即刻失效，新明文仅本次返回）
    /// `POST /mcp/endpoints/{endpoint_id}/token`
    func mcpEndpointsRotateToken(endpointId: String) async throws -> API.EndpointCreatedView {
        return try await send("POST", "/mcp/endpoints/\(endpointId)/token")
    }

    /// MCP 总开关、端点清单与可选服务目录
    /// `GET /mcp/status`
    func mcpStatus() async throws -> API.StatusView {
        return try await send("GET", "/mcp/status")
    }

    /// 开启或关闭 MCP 服务（关闭后所有端点一律 404）
    /// `PUT /mcp/status`
    func mcpToggle(body: API.ToggleRequest) async throws -> API.StatusView {
        return try await send("PUT", "/mcp/status", body: body)
    }

    /// 成员列表（含能力开关与白名单摘要）
    /// `GET /members`
    func membersList() async throws -> [API.MemberView] {
        return try await send("GET", "/members")
    }

    /// 新建成员（把用户名和初始密码发给对方，登录后可自行修改）
    /// `POST /members`
    func membersCreate(body: API.MemberCreateRequest) async throws -> API.MemberView {
        return try await send("POST", "/members", body: body)
    }

    /// 删除成员（清理其个人数据；订阅与已下载内容保留）
    /// `DELETE /members/{member_id}`
    func membersDelete(memberId: Int) async throws -> Void {
        let _: API.JSONValue? = try await send("DELETE", "/members/\(memberId)")
    }

    /// 成员详情
    /// `GET /members/{member_id}`
    func membersShow(memberId: Int) async throws -> API.MemberView {
        return try await send("GET", "/members/\(memberId)")
    }

    /// 编辑成员（昵称 / 能力开关 / 可见库与可用站点白名单）
    /// `PUT /members/{member_id}`
    func membersUpdate(memberId: Int, body: API.MemberUpdateRequest) async throws -> API.MemberView {
        return try await send("PUT", "/members/\(memberId)", body: body)
    }

    /// 重置成员密码（新密码明文仅返回这一次）
    /// `POST /members/{member_id}/reset-password`
    func membersPasswordReset(memberId: Int) async throws -> API.MemberPasswordResetView {
        return try await send("POST", "/members/\(memberId)/reset-password")
    }

    /// 启用 / 停用成员（停用即时踢下线，数据全部保留）
    /// `PUT /members/{member_id}/status`
    func membersStatusSet(memberId: Int, body: API.MemberStatusRequest) async throws -> API.MemberView {
        return try await send("PUT", "/members/\(memberId)/status", body: body)
    }

    /// 读取网络与代理配置
    /// `GET /network/config`
    func netShow() async throws -> API.NetworkConfigView {
        return try await send("GET", "/network/config")
    }

    /// 保存网络与代理配置（立即生效，无需重启）
    /// `PUT /network/config`
    func netSet(body: API.NetworkConfigPayload) async throws -> API.NetworkConfigView {
        return try await send("PUT", "/network/config", body: body)
    }

    /// 按服务做一次连通性测试（走当前保存的出口配置）
    /// `POST /network/test`
    func netTest(body: API.NetworkTestPayload) async throws -> API.NetworkTestResult {
        return try await send("POST", "/network/test", body: body)
    }

    /// 人物页：库内这个影人的档案与作品
    /// `GET /people/{tmdb_person_id}`
    func peopleShow(tmdbPersonId: Int) async throws -> API.PersonView {
        return try await send("GET", "/people/\(tmdbPersonId)")
    }

    /// 看此刻家里谁在看什么、用哪台设备、速度多快
    /// `GET /playback/activity`
    func playbackActivity(scope: String? = nil) async throws -> API.MediaActivityView {
        var query: [URLQueryItem] = []
        if let scope { query.append(URLQueryItem(name: "scope", value: "\(scope)")) }
        return try await send("GET", "/playback/activity", query: query)
    }

    /// 掐断某台设备正在进行的播放（不影响登录）
    /// `POST /playback/activity/sessions/{device_id}/end`
    func playbackActivityEnd(deviceId: String) async throws -> Void {
        let _: API.JSONValue? = try await send("POST", "/playback/activity/sessions/\(deviceId)/end")
    }

    /// 播放器客户端日志
    /// `POST /playback/client-log`
    func playbackClientLog(body: API.PlaybackClientLogPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/playback/client-log", body: body)
    }

    /// 播放决策
    /// `POST /playback/decide`
    func playbackDecide(body: API.PlaybackDecideRequest) async throws -> API.PlaybackDecisionView {
        return try await send("POST", "/playback/decide", body: body)
    }

    /// 注销一台播放设备，让它下次必须重新登录
    /// `DELETE /playback/devices/{device_id}`
    func playbackDeviceRevoke(deviceId: String) async throws -> Void {
        let _: API.JSONValue? = try await send("DELETE", "/playback/devices/\(deviceId)")
    }

    /// 我的收藏
    /// `GET /playback/favorites`
    func playbackFavorites(limit: Int? = nil, offset: Int? = nil, unwatchedFirst: Bool? = nil, sort: String? = nil, order: String? = nil) async throws -> API.FavoritesView {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let offset { query.append(URLQueryItem(name: "offset", value: "\(offset)")) }
        if let unwatchedFirst { query.append(URLQueryItem(name: "unwatched_first", value: "\(unwatchedFirst)")) }
        if let sort { query.append(URLQueryItem(name: "sort", value: "\(sort)")) }
        if let order { query.append(URLQueryItem(name: "order", value: "\(order)")) }
        return try await send("GET", "/playback/favorites", query: query)
    }

    /// 我的收藏 · 图廊：收藏作品的海报 / 剧照 / 章节场景图按作品分组铺平
    /// `GET /playback/favorites/gallery`
    func playbackFavoritesGallery(limit: Int? = nil, offset: Int? = nil, sort: String? = nil, order: String? = nil) async throws -> [API.LibraryGalleryGroupView] {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let offset { query.append(URLQueryItem(name: "offset", value: "\(offset)")) }
        if let sort { query.append(URLQueryItem(name: "sort", value: "\(sort)")) }
        if let order { query.append(URLQueryItem(name: "order", value: "\(order)")) }
        return try await send("GET", "/playback/favorites/gallery", query: query)
    }

    /// 内嵌字体清单
    /// `GET /playback/files/{file_id}/fonts`
    func playbackFileFonts(fileId: Int, token: String) async throws -> API.PlaybackFontsView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "token", value: "\(token)"))
        return try await send("GET", "/playback/files/\(fileId)/fonts", query: query)
    }

    /// 进度条缩略图索引
    /// `GET /playback/files/{file_id}/trickplay`
    func playbackFileTrickplay(fileId: Int, token: String) async throws -> API.TrickplayView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "token", value: "\(token)"))
        return try await send("GET", "/playback/files/\(fileId)/trickplay", query: query)
    }

    /// 硬件加速自检
    /// `GET /playback/hardware`
    func playbackHardwareProbe(refresh: Bool? = nil) async throws -> API.HwProbeView {
        var query: [URLQueryItem] = []
        if let refresh { query.append(URLQueryItem(name: "refresh", value: "\(refresh)")) }
        return try await send("GET", "/playback/hardware", query: query)
    }

    /// 清除自己的观看记录（按条目 / 按库 / 全部）
    /// `DELETE /playback/history`
    func playbackHistoryClear(scope: String, mediaItemId: Int? = nil, libraryId: Int? = nil, since: String? = nil) async throws -> API.PlaybackHistoryClearView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "scope", value: "\(scope)"))
        if let mediaItemId { query.append(URLQueryItem(name: "media_item_id", value: "\(mediaItemId)")) }
        if let libraryId { query.append(URLQueryItem(name: "library_id", value: "\(libraryId)")) }
        if let since { query.append(URLQueryItem(name: "since", value: "\(since)")) }
        return try await send("DELETE", "/playback/history", query: query)
    }

    /// 翻看每一场播放的流水：谁、什么时候、看了什么、看了多久
    /// `GET /playback/history`
    func playbackHistory(limit: Int? = nil, before: Int? = nil, days: Int? = nil, memberId: Int? = nil, scope: String? = nil) async throws -> API.PlaybackHistoryView {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let before { query.append(URLQueryItem(name: "before", value: "\(before)")) }
        if let days { query.append(URLQueryItem(name: "days", value: "\(days)")) }
        if let memberId { query.append(URLQueryItem(name: "member_id", value: "\(memberId)")) }
        if let scope { query.append(URLQueryItem(name: "scope", value: "\(scope)")) }
        return try await send("GET", "/playback/history", query: query)
    }

    /// 播放页条目信息（标题/海报/库归属）
    /// `GET /playback/items/{media_item_id}`
    func playbackItemInfo(mediaItemId: Int) async throws -> API.PlaybackItemView {
        return try await send("GET", "/playback/items/\(mediaItemId)")
    }

    /// 播放页一季的分集清单（切集/上一集下一集数据源）
    /// `GET /playback/items/{media_item_id}/episodes`
    func playbackItemEpisodes(mediaItemId: Int, seasonNumber: Int) async throws -> API.SeasonEpisodesView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "season_number", value: "\(seasonNumber)"))
        return try await send("GET", "/playback/items/\(mediaItemId)/episodes", query: query)
    }

    /// 已看 / 收藏状态
    /// `GET /playback/marks`
    func playbackMarksGet(mediaItemId: Int, seasonNumber: Int? = nil, episodeNumber: Int? = nil) async throws -> API.PlaybackMarksView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "media_item_id", value: "\(mediaItemId)"))
        if let seasonNumber { query.append(URLQueryItem(name: "season_number", value: "\(seasonNumber)")) }
        if let episodeNumber { query.append(URLQueryItem(name: "episode_number", value: "\(episodeNumber)")) }
        return try await send("GET", "/playback/marks", query: query)
    }

    /// 标记已看 / 收藏
    /// `POST /playback/marks`
    func playbackMarksSet(body: API.PlaybackMarksRequest) async throws -> API.PlaybackMarksView {
        return try await send("POST", "/playback/marks", body: body)
    }

    /// 上报播放质量
    /// `POST /playback/metrics`
    func playbackMetricReport(body: API.PlaybackMetricPayload) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/playback/metrics", body: body)
    }

    /// 读取播放策略
    /// `GET /playback/policy`
    func playbackPolicyShow() async throws -> API.PlaybackPolicyView {
        return try await send("GET", "/playback/policy")
    }

    /// 保存播放策略
    /// `PUT /playback/policy`
    func playbackPolicySet(body: API.PlaybackPolicyPayload) async throws -> API.PlaybackPolicyView {
        return try await send("PUT", "/playback/policy", body: body)
    }

    /// 上报观看进度
    /// `POST /playback/progress`
    func playbackProgress(body: API.PlaybackProgressRequest) async throws -> API.PlaybackStateView {
        return try await send("POST", "/playback/progress", body: body)
    }

    /// 续播点与记忆轨
    /// `GET /playback/resume`
    func playbackResume(mediaItemId: Int, seasonNumber: Int? = nil, episodeNumber: Int? = nil) async throws -> API.PlaybackStateView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "media_item_id", value: "\(mediaItemId)"))
        if let seasonNumber { query.append(URLQueryItem(name: "season_number", value: "\(seasonNumber)")) }
        if let episodeNumber { query.append(URLQueryItem(name: "episode_number", value: "\(episodeNumber)")) }
        return try await send("GET", "/playback/resume", query: query)
    }

    /// 开始播放
    /// `POST /playback/sessions`
    func playbackSessionStart(body: API.PlaybackSessionRequest) async throws -> API.PlaybackSessionView {
        return try await send("POST", "/playback/sessions", body: body)
    }

    /// 结束播放
    /// `DELETE /playback/sessions/{session_id}`
    func playbackSessionStop(sessionId: String) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/playback/sessions/\(sessionId)")
    }

    /// 播放会话诊断
    /// `GET /playback/sessions/{session_id}/diagnostics`
    func playbackSessionDiagnostics(sessionId: String, token: String) async throws -> API.PlaybackDiagnosticsView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "token", value: "\(token)"))
        return try await send("GET", "/playback/sessions/\(sessionId)/diagnostics", query: query)
    }

    /// 播放心跳
    /// `POST /playback/sessions/{session_id}/ping`
    func playbackSessionPing(sessionId: String) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/playback/sessions/\(sessionId)/ping")
    }

    /// 播放质量汇总
    /// `GET /playback/stats`
    func playbackStats() async throws -> API.PlaybackStatsView {
        return try await send("GET", "/playback/stats")
    }

    /// 一段时间的观看总览：看了多久、多少场、看完率、活跃了几个人
    /// `GET /playback/stats/watch`
    func playbackStatsWatch(days: Int? = nil, tzOffset: Int? = nil, memberId: Int? = nil, scope: String? = nil) async throws -> API.PlaybackWatchStatsView {
        var query: [URLQueryItem] = []
        if let days { query.append(URLQueryItem(name: "days", value: "\(days)")) }
        if let tzOffset { query.append(URLQueryItem(name: "tz_offset", value: "\(tzOffset)")) }
        if let memberId { query.append(URLQueryItem(name: "member_id", value: "\(memberId)")) }
        if let scope { query.append(URLQueryItem(name: "scope", value: "\(scope)")) }
        return try await send("GET", "/playback/stats/watch", query: query)
    }

    /// 接下来继续
    /// `GET /playback/up-next`
    func playbackUpNext(limit: Int? = nil) async throws -> API.UpNextView {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        return try await send("GET", "/playback/up-next", query: query)
    }

    /// 规则组列表（首次访问自动创建默认组）
    /// `GET /rule-sets`
    func rulesList() async throws -> [API.RuleSetView] {
        return try await send("GET", "/rule-sets")
    }

    /// 创建规则组
    /// `POST /rule-sets`
    func rulesCreate(body: API.RuleSetPayload) async throws -> API.RuleSetView {
        return try await send("POST", "/rule-sets", body: body)
    }

    /// 删除规则组（默认组与被引用的组禁删）
    /// `DELETE /rule-sets/{rule_set_id}`
    func rulesDelete(ruleSetId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/rule-sets/\(ruleSetId)")
    }

    /// 更新规则组（只影响之后的匹配评估）
    /// `PUT /rule-sets/{rule_set_id}`
    func rulesUpdate(ruleSetId: Int, body: API.RuleSetPayload) async throws -> API.RuleSetView {
        return try await send("PUT", "/rule-sets/\(ruleSetId)", body: body)
    }

    /// 设为默认规则组（新订阅未指定规则组时使用）
    /// `POST /rule-sets/{rule_set_id}/default`
    func rulesDefault(ruleSetId: Int) async throws -> API.RuleSetView {
        return try await send("POST", "/rule-sets/\(ruleSetId)/default")
    }

    /// 定时任务：周期、启停与最近/下次执行
    /// `GET /scheduled-tasks`
    func appTasksList() async throws -> [API.ScheduledTaskView] {
        return try await send("GET", "/scheduled-tasks")
    }

    /// 改一个定时任务的周期 / 启停（立即重排，不用重启）
    /// `PUT /scheduled-tasks/{task_key}`
    func appTasksUpdate(taskKey: String, body: API.ScheduledTaskUpdate) async throws -> API.ScheduledTaskView {
        return try await send("PUT", "/scheduled-tasks/\(taskKey)", body: body)
    }

    /// 读取刮削与整理配置
    /// `GET /scrape/config`
    func scrapeShow() async throws -> API.ScrapeConfigView {
        return try await send("GET", "/scrape/config")
    }

    /// 修改刮削与整理配置：只更新给出的字段，没给的保持原样（立即生效）
    /// `PUT /scrape/config`
    func scrapeSet(body: API.MetadataScrapeSettingInput) async throws -> API.ScrapeConfigView {
        return try await send("PUT", "/scrape/config", body: body)
    }

    /// 完整地区表（供「更多地区」搜索面板）
    /// `GET /scrape/country-options`
    func scrapeCountries() async throws -> [API.CountryOption] {
        return try await send("GET", "/scrape/country-options")
    }

    /// 完整语种表（供「更多语言」搜索面板）
    /// `GET /scrape/language-options`
    func scrapeLanguages() async throws -> [API.LanguageOption] {
        return try await send("GET", "/scrape/language-options")
    }

    /// 清空搜索历史
    /// `DELETE /search/history`
    func searchHistoryClear() async throws -> Void {
        let _: API.JSONValue? = try await send("DELETE", "/search/history")
    }

    /// 获取最近的搜索历史
    /// `GET /search/history`
    func searchHistoryList(limit: Int? = nil) async throws -> [API.SearchHistoryItem] {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        return try await send("GET", "/search/history", query: query)
    }

    /// 删除单条搜索历史
    /// `DELETE /search/history/{history_id}`
    func searchHistoryDelete(historyId: Int) async throws -> Void {
        let _: API.JSONValue? = try await send("DELETE", "/search/history/\(historyId)")
    }

    /// 读取一条历史搜索保存的影视条目或种子结果
    /// `GET /search/history/{history_id}/results`
    func searchHistoryGetResults(historyId: Int) async throws -> API.JSONValue {
        return try await send("GET", "/search/history/\(historyId)/results")
    }

    /// 按关键词搜索已入库条目（跨全部媒体库，标题/原名匹配，按库分组）
    /// `GET /search/library-items`
    func searchLibraryItems(keyword: String) async throws -> [API.LibrarySearchGroupView] {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "keyword", value: "\(keyword)"))
        return try await send("GET", "/search/library-items", query: query)
    }

    /// 列出资源搜索的内置分类与自定义站点组合预设
    /// `GET /search/presets`
    func searchPresetsList() async throws -> API.SearchPresetListView {
        return try await send("GET", "/search/presets")
    }

    /// 整体保存资源搜索的分类与站点组合预设
    /// `PUT /search/presets`
    func searchPresetsUpdate(body: API.SearchPresetUpdate) async throws -> API.SearchPresetListView {
        return try await send("PUT", "/search/presets", body: body)
    }

    /// 按片名搜索 TMDB、豆瓣或全部影视来源
    /// `POST /search/titles`
    func searchTitles(body: API.TitleSearchPayload) async throws -> API.TitleSearchView {
        return try await send("POST", "/search/titles", body: body)
    }

    /// 跨站点并发搜索种子资源（关键词留空 = 按分类浏览各站种子列表）
    /// `GET /search/torrents`
    func searchTorrents(keyword: String? = nil, categories: [API.TorrentCategory]? = nil, sites: [String]? = nil, label: String? = nil, noHistory: Bool? = nil, posterMode: Bool? = nil, page: Int? = nil) async throws -> API.SearchResponse {
        var query: [URLQueryItem] = []
        if let keyword { query.append(URLQueryItem(name: "keyword", value: "\(keyword)")) }
        for value in categories ?? [] { query.append(URLQueryItem(name: "categories", value: "\(value)")) }
        for value in sites ?? [] { query.append(URLQueryItem(name: "sites", value: "\(value)")) }
        if let label { query.append(URLQueryItem(name: "label", value: "\(label)")) }
        if let noHistory { query.append(URLQueryItem(name: "no_history", value: "\(noHistory)")) }
        if let posterMode { query.append(URLQueryItem(name: "poster_mode", value: "\(posterMode)")) }
        if let page { query.append(URLQueryItem(name: "page", value: "\(page)")) }
        return try await send("GET", "/search/torrents", query: query)
    }

    /// 最近会话列表（按最后活跃时间倒序）
    /// `GET /sessions`
    func sessionList(limit: Int? = nil, offset: Int? = nil) async throws -> [API.SessionSummary] {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        if let offset { query.append(URLQueryItem(name: "offset", value: "\(offset)")) }
        return try await send("GET", "/sessions", query: query)
    }

    /// 开始新会话，或向已有会话发送用户消息
    /// `POST /sessions`
    func sessionStart(body: API.SessionStartPayload) async throws -> API.SessionMessageAcceptedView {
        return try await send("POST", "/sessions", body: body)
    }

    /// 删除会话（转录文件与索引一并删除）
    /// `DELETE /sessions/{session_id}`
    func sessionDelete(sessionId: String) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/sessions/\(sessionId)")
    }

    /// 读取会话的完整轨迹
    /// `GET /sessions/{session_id}`
    func sessionGetTranscript(sessionId: String) async throws -> API.SessionTranscriptView {
        return try await send("GET", "/sessions/\(sessionId)")
    }

    /// 重命名会话
    /// `PATCH /sessions/{session_id}`
    func sessionRename(sessionId: String, body: API.SessionRenamePayload) async throws -> API.SessionSummary {
        return try await send("PATCH", "/sessions/\(sessionId)", body: body)
    }

    /// 手动压缩会话上下文
    /// `POST /sessions/{session_id}/compact-context`
    func sessionCompactContext(sessionId: String) async throws -> API.SessionContextCompactionView {
        return try await send("POST", "/sessions/\(sessionId)/compact-context")
    }

    /// 从已有上下文创建独立的新会话
    /// `POST /sessions/{session_id}/fork`
    func sessionFork(sessionId: String) async throws -> API.SessionTranscriptView {
        return try await send("POST", "/sessions/\(sessionId)/fork")
    }

    /// 重新提交指定用户消息（可替换问题内容）
    /// `POST /sessions/{session_id}/retry`
    func sessionRetry(sessionId: String, body: API.SessionRetryPayload) async throws -> API.SessionMessageAcceptedView {
        return try await send("POST", "/sessions/\(sessionId)/retry", body: body)
    }

    /// 停止会话当前消息的模型处理
    /// `POST /sessions/{session_id}/stop`
    func sessionStop(sessionId: String) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/sessions/\(sessionId)/stop")
    }

    /// 分享探针：要不要密码、本浏览器是否已解锁
    /// `GET /share/{slug}`
    func shareProbe(slug: String) async throws -> API.SharePublicView {
        return try await send("GET", "/share/\(slug)")
    }

    /// 分享页的合集信息（名字 + 此刻的成员）
    /// `GET /share/{slug}/collection`
    func shareCollection(slug: String) async throws -> API.SharedCollectionView {
        return try await send("GET", "/share/\(slug)/collection")
    }

    /// 分享页一季的分集清单
    /// `GET /share/{slug}/episodes`
    func shareEpisodes(slug: String, seasonNumber: Int, item: Int? = nil) async throws -> API.SeasonEpisodesView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "season_number", value: "\(seasonNumber)"))
        if let item { query.append(URLQueryItem(name: "item", value: "\(item)")) }
        return try await send("GET", "/share/\(slug)/episodes", query: query)
    }

    /// 分享页的影片信息
    /// `GET /share/{slug}/item`
    func shareItem(slug: String, item: Int? = nil) async throws -> API.SharedItemView {
        var query: [URLQueryItem] = []
        if let item { query.append(URLQueryItem(name: "item", value: "\(item)")) }
        return try await send("GET", "/share/\(slug)/item", query: query)
    }

    /// 分享页播放决策
    /// `POST /share/{slug}/playback/decide`
    func sharePlaybackDecide(slug: String, body: API.PlaybackDecideRequest) async throws -> API.PlaybackDecisionView {
        return try await send("POST", "/share/\(slug)/playback/decide", body: body)
    }

    /// 分享页播放器的条目信息
    /// `GET /share/{slug}/playback/items/{media_item_id}`
    func sharePlaybackItem(mediaItemId: Int, slug: String) async throws -> API.PlaybackItemView {
        return try await send("GET", "/share/\(slug)/playback/items/\(mediaItemId)")
    }

    /// 分享页播放器的分集清单
    /// `GET /share/{slug}/playback/items/{media_item_id}/episodes`
    func sharePlaybackItemEpisodes(mediaItemId: Int, slug: String, seasonNumber: Int) async throws -> API.SeasonEpisodesView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "season_number", value: "\(seasonNumber)"))
        return try await send("GET", "/share/\(slug)/playback/items/\(mediaItemId)/episodes", query: query)
    }

    /// 分享页播放心跳（只刷新活动页的实时会话，不落任何观看状态）
    /// `POST /share/{slug}/playback/progress`
    func sharePlaybackProgress(slug: String, body: API.PlaybackProgressRequest) async throws -> API.PlaybackStateView {
        return try await send("POST", "/share/\(slug)/playback/progress", body: body)
    }

    /// 分享页开始播放
    /// `POST /share/{slug}/playback/sessions`
    func sharePlaybackSessionStart(slug: String, body: API.PlaybackSessionRequest) async throws -> API.PlaybackSessionView {
        return try await send("POST", "/share/\(slug)/playback/sessions", body: body)
    }

    /// 分享页结束播放
    /// `DELETE /share/{slug}/playback/sessions/{session_id}`
    func sharePlaybackSessionStop(sessionId: String, slug: String) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/share/\(slug)/playback/sessions/\(sessionId)")
    }

    /// 分享页播放会话诊断
    /// `GET /share/{slug}/playback/sessions/{session_id}/diagnostics`
    func sharePlaybackSessionDiagnostics(sessionId: String, slug: String, token: String) async throws -> API.PlaybackDiagnosticsView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "token", value: "\(token)"))
        return try await send("GET", "/share/\(slug)/playback/sessions/\(sessionId)/diagnostics", query: query)
    }

    /// 分享页播放心跳
    /// `POST /share/{slug}/playback/sessions/{session_id}/ping`
    func sharePlaybackSessionPing(sessionId: String, slug: String) async throws -> [String: API.JSONValue] {
        return try await send("POST", "/share/\(slug)/playback/sessions/\(sessionId)/ping")
    }

    /// 输入分享密码，解锁本浏览器
    /// `POST /share/{slug}/unlock`
    func shareUnlock(slug: String, body: API.ShareUnlockRequest) async throws -> API.SharePublicView {
        return try await send("POST", "/share/\(slug)/unlock", body: body)
    }

    /// 列出当前所有还有效的分享链接
    /// `GET /shares`
    func sharesList() async throws -> [API.ShareView] {
        return try await send("GET", "/shares")
    }

    /// 按 id 取消一条分享，链接立刻失效
    /// `DELETE /shares/{share_id}`
    func sharesRevoke(shareId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/shares/\(shareId)")
    }

    /// 列出用户已配置的站点及验证状态
    /// `GET /sites`
    func siteList() async throws -> [API.ConfiguredSite] {
        return try await send("GET", "/sites")
    }

    /// 配置一个站点（保存后异步验证）
    /// `POST /sites`
    func siteAdd(body: API.SiteConfigCreate) async throws -> API.ConfiguredSite {
        return try await send("POST", "/sites", body: body)
    }

    /// 各站点的刷流运行统计
    /// `GET /sites/boost-stats`
    func siteBoostStats() async throws -> [String: API.SiteBoostStatsView] {
        return try await send("GET", "/sites/boost-stats")
    }

    /// 列出系统支持的可配置站点及授权要求
    /// `GET /sites/catalog`
    func siteCatalog() async throws -> [API.CatalogItem] {
        return try await send("GET", "/sites/catalog")
    }

    /// 各站点的种子缓存量与同步节奏
    /// `GET /sites/sync-stats`
    func siteStats() async throws -> [String: API.SiteSyncStatsView] {
        return try await send("GET", "/sites/sync-stats")
    }

    /// 删除站点配置（连带清理 cookie 缓存）
    /// `DELETE /sites/{site_id}`
    func siteDelete(siteId: String) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/sites/\(siteId)")
    }

    /// 获取单个已配置站点详情
    /// `GET /sites/{site_id}`
    func siteShow(siteId: String) async throws -> API.ConfiguredSite {
        return try await send("GET", "/sites/\(siteId)")
    }

    /// 更新站点授权信息（更新后重新异步验证）
    /// `PUT /sites/{site_id}`
    func siteUpdate(siteId: String, body: API.SiteConfigUpdate) async throws -> API.ConfiguredSite {
        return try await send("PUT", "/sites/\(siteId)", body: body)
    }

    /// 打开 / 关闭站点保护
    /// `PATCH /sites/{site_id}/protection`
    func siteProtectionSet(siteId: String, body: API.SiteProtectionUpdate) async throws -> API.ConfiguredSite {
        return try await send("PATCH", "/sites/\(siteId)/protection", body: body)
    }

    /// 设置自动刷分享率（开关与存储预算）
    /// `PATCH /sites/{site_id}/ratio-boost`
    func siteRatioBoostSet(siteId: String, body: API.SiteRatioBoostUpdate) async throws -> API.ConfiguredSite {
        return try await send("PATCH", "/sites/\(siteId)/ratio-boost", body: body)
    }

    /// 暂停 / 恢复站点刷流
    /// `PATCH /sites/{site_id}/ratio-boost/pause`
    func siteRatioBoostPause(siteId: String, body: API.SiteBoostPauseUpdate) async throws -> API.ConfiguredSite {
        return try await send("PATCH", "/sites/\(siteId)/ratio-boost/pause", body: body)
    }

    /// 启用 / 停用站点
    /// `PATCH /sites/{site_id}/status`
    func siteStatusSet(siteId: String, body: API.SiteStatusUpdate) async throws -> API.ConfiguredSite {
        return try await send("PATCH", "/sites/\(siteId)/status", body: body)
    }

    /// 手动重新触发验证
    /// `POST /sites/{site_id}/verify`
    func siteVerify(siteId: String) async throws -> API.ConfiguredSite {
        return try await send("POST", "/sites/\(siteId)/verify")
    }

    /// 列出可显式调用的 Agent 技能
    /// `GET /skills`
    func skillsList() async throws -> [API.SkillView] {
        return try await send("GET", "/skills")
    }

    /// 列出当前账号可见的电影和剧集订阅
    /// `GET /subscriptions`
    func subscriptionsList(kind: String? = nil) async throws -> [API.SubscriptionView] {
        var query: [URLQueryItem] = []
        if let kind { query.append(URLQueryItem(name: "kind", value: "\(kind)")) }
        return try await send("GET", "/subscriptions", query: query)
    }

    /// 从 Discover 影视条目创建订阅并生成初始追踪工单
    /// `POST /subscriptions`
    func subscriptionsCreate(body: API.SubscriptionCreatePayload) async throws -> API.SubscriptionCreateView {
        return try await send("POST", "/subscriptions", body: body)
    }

    /// 检查订阅自动搜索、下载、转移与入库链路是否就绪
    /// `GET /subscriptions/automation-readiness`
    func subscriptionsCheckAutomationReadiness() async throws -> API.PipelineHealthView {
        return try await send("GET", "/subscriptions/automation-readiness")
    }

    /// 预览订阅资源的下载目标与自动入库路径
    /// `GET /subscriptions/download-routing-preview`
    func subscriptionsPreviewDownloadRouting(kind: String, libraryId: Int? = nil, tmdbId: Int? = nil, title: String? = nil, year: Int? = nil) async throws -> API.DispatchPreviewView {
        var query: [URLQueryItem] = []
        query.append(URLQueryItem(name: "kind", value: "\(kind)"))
        if let libraryId { query.append(URLQueryItem(name: "library_id", value: "\(libraryId)")) }
        if let tmdbId { query.append(URLQueryItem(name: "tmdb_id", value: "\(tmdbId)")) }
        if let title { query.append(URLQueryItem(name: "title", value: "\(title)")) }
        if let year { query.append(URLQueryItem(name: "year", value: "\(year)")) }
        return try await send("GET", "/subscriptions/download-routing-preview", query: query)
    }

    /// 预览订阅目标的季集、库存和现有订阅状态
    /// `POST /subscriptions/title-preview`
    func uiSubscriptionsPreviewTitle(body: API.SubscriptionTargetPreviewPayload) async throws -> API.PrepareView {
        return try await send("POST", "/subscriptions/title-preview", body: body)
    }

    /// 列出最近一次可能入库的订阅内容
    /// `GET /subscriptions/today-arrivals`
    func subscriptionsListTodayArrivals() async throws -> [API.TodayArrivalView] {
        return try await send("GET", "/subscriptions/today-arrivals")
    }

    /// 管理员永久删除一条订阅及其追踪工单（默认不删除已下载内容）
    /// `DELETE /subscriptions/{subscription_id}`
    func subscriptionsDelete(subscriptionId: Int, deleteTorrents: Bool? = nil, deleteLibraryFiles: Bool? = nil) async throws -> API.SubscriptionDeleteView {
        var query: [URLQueryItem] = []
        if let deleteTorrents { query.append(URLQueryItem(name: "delete_torrents", value: "\(deleteTorrents)")) }
        if let deleteLibraryFiles { query.append(URLQueryItem(name: "delete_library_files", value: "\(deleteLibraryFiles)")) }
        return try await send("DELETE", "/subscriptions/\(subscriptionId)", query: query)
    }

    /// 获取一条订阅的设置、进度和缺失资源明细
    /// `GET /subscriptions/{subscription_id}`
    func subscriptionsGet(subscriptionId: Int) async throws -> API.SubscriptionDetailView {
        return try await send("GET", "/subscriptions/\(subscriptionId)")
    }

    /// 修改订阅的选季、自动续订、过滤规则或目标媒体库
    /// `PATCH /subscriptions/{subscription_id}`
    func subscriptionsUpdate(subscriptionId: Int, body: API.SubscriptionUpdatePayload) async throws -> API.SubscriptionDetailView {
        return try await send("PATCH", "/subscriptions/\(subscriptionId)", body: body)
    }

    /// 列出一条订阅当前正在进行的下载及实时进度
    /// `GET /subscriptions/{subscription_id}/active-downloads`
    func subscriptionsListActiveDownloads(subscriptionId: Int) async throws -> [API.SubscriptionDownloadView] {
        return try await send("GET", "/subscriptions/\(subscriptionId)/active-downloads")
    }

    /// 按时间倒序列出一条订阅的活动记录
    /// `GET /subscriptions/{subscription_id}/activities`
    func subscriptionsListActivities(subscriptionId: Int, limit: Int? = nil) async throws -> [API.ActivityView] {
        var query: [URLQueryItem] = []
        if let limit { query.append(URLQueryItem(name: "limit", value: "\(limit)")) }
        return try await send("GET", "/subscriptions/\(subscriptionId)/activities", query: query)
    }

    /// 开启或关闭一条剧集订阅的自动续订
    /// `PATCH /subscriptions/{subscription_id}/follow-future`
    func subscriptionsSetFollowFuture(subscriptionId: Int, body: API.SubscriptionFollowFuturePayload) async throws -> API.SubscriptionDetailView {
        return try await send("PATCH", "/subscriptions/\(subscriptionId)/follow-future", body: body)
    }

    /// 成员停止关注一条订阅而不影响其他正在追踪的成员
    /// `DELETE /subscriptions/{subscription_id}/following`
    func subscriptionsUnsubscribe(subscriptionId: Int) async throws -> [String: API.JSONValue] {
        return try await send("DELETE", "/subscriptions/\(subscriptionId)/following")
    }

    /// 立即为一条订阅搜索目前缺失且已经可搜索的资源
    /// `POST /subscriptions/{subscription_id}/missing-resource-searches`
    func subscriptionsSearchMissingResources(subscriptionId: Int) async throws -> API.SearchNowView {
        return try await send("POST", "/subscriptions/\(subscriptionId)/missing-resource-searches")
    }

    /// 取消订阅前预览可一并清理的种子与媒体库文件
    /// `GET /subscriptions/{subscription_id}/removal-preview`
    func subscriptionsPreviewRemoval(subscriptionId: Int, seasons: [Int]? = nil) async throws -> API.SubscriptionRemovalPreviewView {
        var query: [URLQueryItem] = []
        for value in seasons ?? [] { query.append(URLQueryItem(name: "seasons", value: "\(value)")) }
        return try await send("GET", "/subscriptions/\(subscriptionId)/removal-preview", query: query)
    }

    /// 清理已移出订阅范围的那几季的种子与媒体库文件
    /// `POST /subscriptions/{subscription_id}/season-cleanup`
    func subscriptionsCleanupSeasons(subscriptionId: Int, body: API.SeasonCleanupPayload) async throws -> API.SubscriptionDeleteView {
        return try await send("POST", "/subscriptions/\(subscriptionId)/season-cleanup", body: body)
    }

    /// 下载为一条订阅人工选中的种子搜索结果
    /// `POST /subscriptions/{subscription_id}/selected-torrent-downloads`
    func subscriptionsDownloadSelectedTorrent(subscriptionId: Int, body: API.GrabPayload) async throws -> API.GrabResultView {
        return try await send("POST", "/subscriptions/\(subscriptionId)/selected-torrent-downloads", body: body)
    }

    /// 把一条订阅的追踪状态明确设置为 active 或 paused
    /// `PATCH /subscriptions/{subscription_id}/tracking-state`
    func subscriptionsSetTrackingState(subscriptionId: Int, body: API.SubscriptionTrackingStatePayload) async throws -> API.SubscriptionDetailView {
        return try await send("PATCH", "/subscriptions/\(subscriptionId)/tracking-state", body: body)
    }

    /// 触发一轮洗版：逐集体检并把可洗单元排入立即搜索
    /// `POST /subscriptions/{subscription_id}/upgrade-runs`
    func subscriptionsUpgradeRun(subscriptionId: Int, body: API.UpgradeRunPayload) async throws -> API.UpgradeRunView {
        return try await send("POST", "/subscriptions/\(subscriptionId)/upgrade-runs", body: body)
    }

    /// 列出可查看的日志日期
    /// `GET /system/logs`
    func logsDays() async throws -> API.LogDayList {
        return try await send("GET", "/system/logs")
    }

    /// 读取某天的日志内容
    /// `GET /system/logs/{day}`
    func logsRead(day: String, tail: Int? = nil) async throws -> API.LogContent {
        var query: [URLQueryItem] = []
        if let tail { query.append(URLQueryItem(name: "tail", value: "\(tail)")) }
        return try await send("GET", "/system/logs/\(day)", query: query)
    }

    /// 列出全部活跃的待处理事项（error 在前，新的在前）
    /// `GET /system/notices`
    func noticesList() async throws -> [API.NoticeView] {
        return try await send("GET", "/system/notices")
    }

    /// 忽略一条待处理事项（问题仍在也不再提示；自动消退不受影响）
    /// `POST /system/notices/{notice_id}/dismiss`
    func noticesDismiss(noticeId: Int) async throws -> Void {
        let _: API.JSONValue? = try await send("POST", "/system/notices/\(noticeId)/dismiss")
    }

    /// 读取远程转码配置
    /// `GET /transcode-worker/config`
    func transcodeConfigShow() async throws -> API.RemoteTranscodeConfigView {
        return try await send("GET", "/transcode-worker/config")
    }

    /// 保存远程转码配置
    /// `PUT /transcode-worker/config`
    func transcodeConfigSet(body: API.RemoteTranscodeConfigPayload) async throws -> API.RemoteTranscodeConfigView {
        return try await send("PUT", "/transcode-worker/config", body: body)
    }

    /// 远程转码 Worker 状态
    /// `GET /transcode-worker/status`
    func transcodeStatus() async throws -> [String: API.JSONValue] {
        return try await send("GET", "/transcode-worker/status")
    }

    /// 发现页展示编排（分区引用与呈现方式）
    /// `GET /ui/discovery/{media_type}`
    func uiDiscoveryGet(mediaType: API.MediaKind, provider: API.MediaSource? = nil) async throws -> API.DiscoveryPageView {
        var query: [URLQueryItem] = []
        if let provider { query.append(URLQueryItem(name: "provider", value: "\(provider)")) }
        return try await send("GET", "/ui/discovery/\(mediaType)", query: query)
    }

    /// 读取界面偏好（按页面分组的样式设定）
    /// `GET /ui/preferences`
    func uiPrefsShow() async throws -> API.UiPreferencesSetting {
        return try await send("GET", "/ui/preferences")
    }

    /// 保存界面偏好（整体覆盖）
    /// `PUT /ui/preferences`
    func uiPrefsUpdate(body: API.UiPreferencesSettingInput) async throws -> API.UiPreferencesSetting {
        return try await send("PUT", "/ui/preferences", body: body)
    }

    /// 读取 Webhook 配置与事件目录
    /// `GET /webhook`
    func webhookShow() async throws -> API.WebhookConfigView {
        return try await send("GET", "/webhook")
    }

    /// 保存 Webhook 配置（新建 endpoint 的 secret 仅本次返回明文）
    /// `PUT /webhook`
    func webhookSet(body: API.WebhookConfigPayload) async throws -> API.WebhookConfigView {
        return try await send("PUT", "/webhook", body: body)
    }

    /// 最近投递记录（每端点保留 50 条，重启清空）
    /// `GET /webhook/endpoints/{endpoint_id}/deliveries`
    func webhookDeliveries(endpointId: String) async throws -> [API.DeliveryView] {
        return try await send("GET", "/webhook/endpoints/\(endpointId)/deliveries")
    }

    /// 轮换签名密钥（旧密钥即刻作废，新明文仅本次返回）
    /// `POST /webhook/endpoints/{endpoint_id}/rotate-secret`
    func webhookRotateSecret(endpointId: String) async throws -> API.WebhookEndpointView {
        return try await send("POST", "/webhook/endpoints/\(endpointId)/rotate-secret")
    }

    /// 发送测试事件（同步返回状态码/耗时/错误）
    /// `POST /webhook/endpoints/{endpoint_id}/test`
    func webhookTest(endpointId: String) async throws -> API.DeliveryView {
        return try await send("POST", "/webhook/endpoints/\(endpointId)/test")
    }

}

// 未生成的接口（需在对应模块手写）：
// - POST /api/v1/appearance/backdrops（multipart 表单上传，需手写）
// - GET /api/v1/appearance/backdrops/{backdrop_id}（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/auth/avatar（无响应模型：文件流/SSE 等，需手写）
// - POST /api/v1/auth/avatar（multipart 表单上传，需手写）
// - GET /api/v1/images/assets/{path:path}（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/images/proxy（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/jobs/stream（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/libraries/files/{file_id}/original（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/libraries/files/{file_id}/thumb（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/libraries/{library_id}/cover（无响应模型：文件流/SSE 等，需手写）
// - POST /api/v1/libraries/{library_id}/cover（multipart 表单上传，需手写）
// - GET /api/v1/libraries/{library_id}/items/{media_item_id}/artwork（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/members/{member_id}/avatar（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/playback/files/{file_id}/fonts/{name}（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/playback/files/{file_id}/stream（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/playback/files/{file_id}/subtitles（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/playback/files/{file_id}/trickplay/{name}（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/playback/sessions/{session_id}/index.m3u8（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/playback/sessions/{session_id}/master.m3u8（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/playback/sessions/{session_id}/sub{index}.m3u8（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/playback/sessions/{session_id}/{name}（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/search/torrents/stream（无响应模型：文件流/SSE 等，需手写）
// - POST /api/v1/sessions/attachments（multipart 表单上传，需手写）
// - GET /api/v1/sessions/{session_id}/attachments/{attachment_id}（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/sessions/{session_id}/events（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/share/{slug}/artwork（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/share/{slug}/files/{file_id}/thumb（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/share/{slug}/images/assets/{path:path}（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/share/{slug}/images/proxy（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/spec（无响应模型：文件流/SSE 等，需手写）
// - PUT /api/v1/transcode-worker/sessions/{session_id}/artifacts/{name}（无响应模型：文件流/SSE 等，需手写）
// - GET /api/v1/transcode-worker/sessions/{session_id}/source（无响应模型：文件流/SSE 等，需手写）
