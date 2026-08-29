package tree

// 域分组的一行产品能力简介（命令组的 help；生成命令自身的参数与详细说明来自
// spec）。模型会先看 mclaw 工具目录、再用域级 --help 探索命令，因此这里要用
// 用户语义区分相邻域，不能只重复内部对象名。
//
// 本文件由 Python 版逐字导出，两边内容必须一致（有守护测试）。
var domainHelp = map[string]string{
	"app":           "外部访问设置、应用重启、版本升级/回退与 NER 模型更新",
	"appearance":    "首页背景与图库",
	"auth":          "个人信息、会话与 API 令牌",
	"channels":      "微信、Telegram、Discord 消息推送与 AI 对话入口",
	"discover":      "浏览 TMDB/豆瓣电影与剧集片单，并读取影视条目完整资料",
	"dl":            "qBittorrent/Transmission 下载器、路径映射与种子投递",
	"extension":     "浏览器插件 Cookie 同步",
	"health":        "API 存活检查",
	"jobs":          "后台作业查询、事件、等待、取消与重试",
	"library":       "管理本地电影/剧集媒体库、库存文件、识别结果、元数据、图片与字幕；organize-files 按 scrape 配的命名模板批量整理存量文件名",
	"llm":           "OpenAI、阿里云百炼及 OpenAI 兼容模型接入与验证",
	"logs":          "系统日志查看与实时跟随",
	"members":       "家庭成员账号、能力开关与可见范围",
	"net":           "全局/指定服务代理、镜像地址与连通性测试",
	"notices":       "系统待处理事项",
	"people":        "本地媒体库影人档案与作品",
	"rules":         "订阅资源质量与过滤规则组",
	"scrape":        "刮削与整理配置：元数据语言优先级、选图口味与图片档位、目录与文件名模板、媒体目录写入开关",
	"search":        "统一搜索影视条目、PT 种子和本地媒体库，并管理搜索预设与历史",
	"session":       "AI 会话开始/继续、指定消息重试、SSE 跟随、完整轨迹与上下文管理",
	"site":          "PT 资源站点接入、鉴权验证与缓存状态",
	"subscriptions": "持续追踪电影/剧集缺失资源并自动搜索、下载和整理入库",
	"transcode":     "远程硬件转码 Worker 配置与状态",
	"ui":            "Web 界面质感、布局与显示偏好",
	"watch":         "下载完成目录监听、自动识别、标准命名与入库",
	"webhook":       "播放、收藏等事件的 Webhook 推送与投递记录",
}

// 二级分组同样是模型的探索入口，必须说明「这里解决什么问题」。只给一级域描述
// 会让 items / identification / metadata 等孤立名词失去选择依据。
var commandGroupHelp = map[string]string{
	"library.artwork":        "查看、下载和选定媒体条目的海报或背景图",
	"library.identification": "处理待识别、错识别和已忽略文件，明确指定文件所属影视条目",
	"library.items":          "查看和管理已经入库的电影、剧集条目及其物理文件",
	"library.metadata":       "刷新整个媒体库的 TMDB 元数据并查看或停止刷新任务",
	"library.missing":        "查看磁盘上已经缺失的库存记录、重新下载或清理台账",
	"library.scan":           "扫描媒体库根路径，把存量文件识别并登记到库存台账",
	"library.subtitles":      "预检和生成 AI 字幕，或校准外挂字幕时间轴",
	"search.history":         "列出、回放、删除或清空影视条目与 PT 种子搜索历史",
	"search.presets":         "列出或更新 PT 种子搜索的分类与站点组合预设",
}
