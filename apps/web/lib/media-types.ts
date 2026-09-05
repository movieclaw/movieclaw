/**
 * 影视数据的前端核心类型（发现页 / 详情页 / 订阅页共用）。
 *
 * 这些形态即组件的渲染契约：后端 /discover 接口返回 snake_case 字段，
 * 由 lib/api/discover.ts 映射成这里的 camelCase；lib/mock-media.ts 的
 * 遗留模拟数据（订阅页仍在用）也遵循同一套类型。
 */

export type MediaType = "movie" | "tv";
/** 媒体库的内容形态：发现/订阅只认 movie/tv，库还多一种「其他」（video，
 *  无结构假设的本地视频，见 docs/design/library-other-kind.md）。 */
export type LibraryKind = MediaType | "video";
/** 库内条目的身份来源：tmdb=有外部档案；local=本地内容或影视库里尚未识别的文件。 */
export type ItemSource = "tmdb" | "local";
export const LIBRARY_KIND_LABELS: Record<LibraryKind, string> = {
  movie: "电影",
  tv: "剧集",
  video: "其他",
};
export type MediaSource = "tmdb" | "douban";

/** 发现列表的轻量库存摘要；只用于入库标记，不携带文件详情。 */
export interface MediaLibraryStatus {
  mediaItemId: number;
  libraryCount: number;
  fileCount: number;
}

/** 发现详情页跳转到已有媒体库条目所需的稳定身份。 */
export interface MediaLibraryLink {
  libraryId: number;
  libraryName: string;
  mediaItemId: number;
}

/** 海报 hover 的两行紧凑上下文；用于补充卡片底部不常显的信息。 */
export interface MediaOverlayDetails {
  primary: string;
  secondary?: string;
}

export interface MediaItem {
  /** 服务端签发的稳定引用；站内跳详情时优先原样使用。 */
  titleRef?: string;
  /** 来源站条目 ID（字符串形态，仅用于路由展示与现有业务兼容） */
  id: string;
  /** 来源与 id 共同构成媒体条目的稳定身份 */
  source?: MediaSource;
  type: MediaType;
  /** 主图宽高比；缺省 2:3。媒体库里本地抓帧的缩略图是 16:9（见 LibraryItem.primary_aspect） */
  aspect?: number;
  /** 中文标题 */
  title: string;
  /** 原名（拉丁/原语言） */
  originalTitle: string;
  year: number;
  /** 评分（0~10，一位小数）；0 表示暂无评分 */
  rating: number;
  /** 类型标签，如「科幻 / 冒险」 */
  genres: string[];
  /** 规模：电影为时长，剧集为季数；列表数据为空，进详情后回填 */
  extent: string;
  /** 站点资源质量徽章（清晰度 / HDR / 字幕）；预留给资源匹配，当前为空 */
  badges: string[];
  /** 一句话简介（卡片 hover 与 Hero 横幅展示） */
  overview: string;
  /** hover 专用的紧凑上下文，不进入海报下方的常显元信息。 */
  overlayDetails?: MediaOverlayDetails;
  posterUrl: string;
  /** 仅 Hero 精选项需要的宽幅背景图 */
  backdropUrl?: string;
  /** 有在位文件时的库存摘要；无匹配或旧接口响应时为空。 */
  libraryStatus?: MediaLibraryStatus | null;
}

/** 一行横滚海报的分类数据 */
export interface MediaRowData {
  id: string;
  title: string;
  items: MediaItem[];
}
