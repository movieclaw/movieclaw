// 由 apps/apple/scripts/gen_api.py 生成，勿手改。
// 需要一台运行中的 MovieClaw；设置环境变量 MC_LIVE=1 才会执行
// （xcodebuild 传 TEST_RUNNER_MC_LIVE=1），地址/账号见 LiveServer。
import Testing
@testable import MovieClaw

@Suite("生成模型对真实服务器解码", .enabled(if: LiveServer.enabled), .serialized)
struct LiveDecodeTests {
    @Test func appShow() async throws {
        try await LiveServer.check { try await $0.appShow() }
    }
    @Test func appStorageUsage() async throws {
        try await LiveServer.check { try await $0.appStorageUsage() }
    }
    @Test func appUpdatePending() async throws {
        try await LiveServer.check { try await $0.appUpdatePending() }
    }
    @Test func appUpdateProgress() async throws {
        try await LiveServer.check { try await $0.appUpdateProgress() }
    }
    @Test func appUpdateRollbackOptions() async throws {
        try await LiveServer.check { try await $0.appUpdateRollbackOptions() }
    }
    @Test func appUpdateStatus() async throws {
        try await LiveServer.check { try await $0.appUpdateStatus() }
    }
    @Test func appearanceShow() async throws {
        try await LiveServer.check { try await $0.appearanceShow() }
    }
    @Test func authAccountsList() async throws {
        try await LiveServer.check { try await $0.authAccountsList() }
    }
    @Test func authBootstrapStatus() async throws {
        try await LiveServer.check { try await $0.authBootstrapStatus() }
    }
    @Test func authDevicesRequests() async throws {
        try await LiveServer.check { try await $0.authDevicesRequests() }
    }
    @Test func authMe() async throws {
        try await LiveServer.check { try await $0.authMe() }
    }
    @Test func authTokensList() async throws {
        try await LiveServer.check { try await $0.authTokensList() }
    }
    @Test func channelsImPushConfigGet() async throws {
        try await LiveServer.check { try await $0.channelsImPushConfigGet() }
    }
    @Test func channelsWeixinAccountsList() async throws {
        try await LiveServer.check { try await $0.channelsWeixinAccountsList() }
    }
    @Test func collectionList() async throws {
        try await LiveServer.check { try await $0.collectionList() }
    }
    @Test func discoverRegionShow() async throws {
        try await LiveServer.check { try await $0.discoverRegionShow() }
    }
    @Test func dlList() async throws {
        try await LiveServer.check { try await $0.dlList() }
    }
    @Test func dlTargetPrefsList() async throws {
        try await LiveServer.check { try await $0.dlTargetPrefsList() }
    }
    @Test func dlTasks() async throws {
        try await LiveServer.check { try await $0.dlTasks() }
    }
    @Test func extensionPing() async throws {
        try await LiveServer.check { try await $0.extensionPing() }
    }
    @Test func extensionSitesList() async throws {
        try await LiveServer.check { try await $0.extensionSitesList() }
    }
    @Test func extensionTokenShow() async throws {
        try await LiveServer.check { try await $0.extensionTokenShow() }
    }
    @Test func fsBrowse() async throws {
        try await LiveServer.check { try await $0.fsBrowse() }
    }
    @Test func healthCheck() async throws {
        try await LiveServer.check { try await $0.healthCheck() }
    }
    @Test func watchList() async throws {
        try await LiveServer.check { try await $0.watchList() }
    }
    @Test func jobsList() async throws {
        try await LiveServer.check { try await $0.jobsList() }
    }
    @Test func jobsHealth() async throws {
        try await LiveServer.check { try await $0.jobsHealth() }
    }
    @Test func libraryList() async throws {
        try await LiveServer.check { try await $0.libraryList() }
    }
    @Test func libraryDuplicatesList() async throws {
        try await LiveServer.check { try await $0.libraryDuplicatesList() }
    }
    @Test func libraryIdentificationListIgnoredFiles() async throws {
        try await LiveServer.check { try await $0.libraryIdentificationListIgnoredFiles() }
    }
    @Test func libraryIdentificationListReviewCases() async throws {
        try await LiveServer.check { try await $0.libraryIdentificationListReviewCases() }
    }
    @Test func libraryIdentificationListUnidentifiedFiles() async throws {
        try await LiveServer.check { try await $0.libraryIdentificationListUnidentifiedFiles() }
    }
    @Test func libraryListRoutingOptions() async throws {
        try await LiveServer.check { try await $0.libraryListRoutingOptions() }
    }
    @Test func libraryRecycleList() async throws {
        try await LiveServer.check { try await $0.libraryRecycleList() }
    }
    @Test func llmDefaultsShow() async throws {
        try await LiveServer.check { try await $0.llmDefaultsShow() }
    }
    @Test func llmModels() async throws {
        try await LiveServer.check { try await $0.llmModels() }
    }
    @Test func llmPresets() async throws {
        try await LiveServer.check { try await $0.llmPresets() }
    }
    @Test func llmProvidersList() async throws {
        try await LiveServer.check { try await $0.llmProvidersList() }
    }
    @Test func mcpStatus() async throws {
        try await LiveServer.check { try await $0.mcpStatus() }
    }
    @Test func membersList() async throws {
        try await LiveServer.check { try await $0.membersList() }
    }
    @Test func netShow() async throws {
        try await LiveServer.check { try await $0.netShow() }
    }
    @Test func playbackActivity() async throws {
        try await LiveServer.check { try await $0.playbackActivity() }
    }
    @Test func playbackFavorites() async throws {
        try await LiveServer.check { try await $0.playbackFavorites() }
    }
    @Test func playbackFavoritesGallery() async throws {
        try await LiveServer.check { try await $0.playbackFavoritesGallery() }
    }
    @Test func playbackHardwareProbe() async throws {
        try await LiveServer.check { try await $0.playbackHardwareProbe() }
    }
    @Test func playbackHistory() async throws {
        try await LiveServer.check { try await $0.playbackHistory() }
    }
    @Test func playbackPolicyShow() async throws {
        try await LiveServer.check { try await $0.playbackPolicyShow() }
    }
    @Test func playbackStats() async throws {
        try await LiveServer.check { try await $0.playbackStats() }
    }
    @Test func playbackStatsWatch() async throws {
        try await LiveServer.check { try await $0.playbackStatsWatch() }
    }
    @Test func playbackUpNext() async throws {
        try await LiveServer.check { try await $0.playbackUpNext() }
    }
    @Test func rulesList() async throws {
        try await LiveServer.check { try await $0.rulesList() }
    }
    @Test func appTasksList() async throws {
        try await LiveServer.check { try await $0.appTasksList() }
    }
    @Test func scrapeShow() async throws {
        try await LiveServer.check { try await $0.scrapeShow() }
    }
    @Test func scrapeCountries() async throws {
        try await LiveServer.check { try await $0.scrapeCountries() }
    }
    @Test func scrapeLanguages() async throws {
        try await LiveServer.check { try await $0.scrapeLanguages() }
    }
    @Test func searchHistoryList() async throws {
        try await LiveServer.check { try await $0.searchHistoryList() }
    }
    @Test func searchPresetsList() async throws {
        try await LiveServer.check { try await $0.searchPresetsList() }
    }
    @Test func searchTorrents() async throws {
        try await LiveServer.check { try await $0.searchTorrents() }
    }
    @Test func sessionList() async throws {
        try await LiveServer.check { try await $0.sessionList() }
    }
    @Test func sharesList() async throws {
        try await LiveServer.check { try await $0.sharesList() }
    }
    @Test func siteList() async throws {
        try await LiveServer.check { try await $0.siteList() }
    }
    @Test func siteBoostPoolShow() async throws {
        try await LiveServer.check { try await $0.siteBoostPoolShow() }
    }
    @Test func siteBoostStats() async throws {
        try await LiveServer.check { try await $0.siteBoostStats() }
    }
    @Test func siteCatalog() async throws {
        try await LiveServer.check { try await $0.siteCatalog() }
    }
    @Test func siteStats() async throws {
        try await LiveServer.check { try await $0.siteStats() }
    }
    @Test func skillsList() async throws {
        try await LiveServer.check { try await $0.skillsList() }
    }
    @Test func subscriptionsList() async throws {
        try await LiveServer.check { try await $0.subscriptionsList() }
    }
    @Test func subscriptionsCheckAutomationReadiness() async throws {
        try await LiveServer.check { try await $0.subscriptionsCheckAutomationReadiness() }
    }
    @Test func subscriptionsListRecentArrivals() async throws {
        try await LiveServer.check { try await $0.subscriptionsListRecentArrivals() }
    }
    @Test func subscriptionsListTodayArrivals() async throws {
        try await LiveServer.check { try await $0.subscriptionsListTodayArrivals() }
    }
    @Test func logsDays() async throws {
        try await LiveServer.check { try await $0.logsDays() }
    }
    @Test func noticesList() async throws {
        try await LiveServer.check { try await $0.noticesList() }
    }
    @Test func transcodeConfigShow() async throws {
        try await LiveServer.check { try await $0.transcodeConfigShow() }
    }
    @Test func transcodeStatus() async throws {
        try await LiveServer.check { try await $0.transcodeStatus() }
    }
    @Test func uiPrefsShow() async throws {
        try await LiveServer.check { try await $0.uiPrefsShow() }
    }
    @Test func webhookShow() async throws {
        try await LiveServer.check { try await $0.webhookShow() }
    }
}
