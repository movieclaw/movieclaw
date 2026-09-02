/**
 * Agent 生成式 UI：把 show_media_cards 工具调用的参数解析成可渲染的卡片规格。
 *
 * 机制（docs/design/agent-generative-ui.md，思路对齐 AG-UI 的 render-only tool）：
 * 后端工具只校验参数并回一句固定回执；前端拦截到该工具的 tool_call 后按参数
 * 里的编号自行调接口加载数据并绘制卡片。流式与历史回放走同一条路径——转录里
 * 的 assistant.tool_calls 与实时 tool_call 事件都会经过这里。
 *
 * 参数字段名与 mclaw 的输出字段一致（title_ref / media_item_id / season_number /
 * subscription_id …），模型从命令结果里原样搬编号，这里也按同名字段读。
 *
 * 版本：工具名带 `_v1` 后缀，是渲染器的匹配键。参数契约不兼容变更时后端发
 * `_v2`，这里给新版本单独写解析器；旧会话里的 `_v1` 调用继续按旧规则绘制。
 * 完全认不出的名字（更新的后端 / 被删的版本）返回 null，会话页退回普通的
 * 工具调用行，不会因为一张卡片认不出而整轮渲染失败。
 *
 * 本文件保持纯逻辑、零 React 依赖：apps/web/test 用 node --test 直接导入。
 */

import type { MediaType } from "@/lib/media-types";

/** 当前前端能绘制的版本；一个名字对应一套参数解析规则。 */
export const MEDIA_CARDS_TOOL_V1 = "show_media_cards_v1";

export type MediaCardSpec =
  | { kind: "library"; key: string; libraryId: number }
  | {
      kind: "title";
      key: string;
      /** 服务端稳定引用：tmdb:movie:123 / douban:456，详情接口原样消费 */
      titleRef: string;
    }
  | {
      kind: "library_item";
      key: string;
      mediaItemId: number;
      /** 剧集指定播放哪一集（两者成对出现） */
      seasonNumber?: number;
      episodeNumber?: number;
    }
  | { kind: "subscription"; key: string; subscriptionId: number };

export interface MediaCardGroup {
  /** 卡片组类型；同一次调用只画一种组件 */
  component: MediaCardSpec["kind"];
  /** 可选的小标题（模型给的一句话，如「你可能会喜欢」） */
  title?: string;
  cards: MediaCardSpec[];
}

const COMPONENTS: ReadonlySet<string> = new Set(["library", "title", "library_item", "subscription"]);

/** 与后端 parse_title_ref 同一口径：tmdb 三段式或豆瓣两段式。 */
const TITLE_REF = /^(tmdb:(movie|tv):\d+|douban:[^:/\s]+)$/;

/** 判断一个工具调用是否由本渲染器负责（任何已知版本）。 */
export function isMediaCardsTool(name: string): boolean {
  return name === MEDIA_CARDS_TOOL_V1;
}

function positiveInt(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null;
}

/**
 * 把工具参数解析成卡片组。参数来自模型生成，任何字段都可能缺失或错型：
 * 单项不合法就跳过那一项（后端 handler 会把错误回喂模型，模型通常会重发），
 * 整组没有一张可画时返回 null，调用方按普通工具行展示。
 */
export function parseMediaCardsArgs(
  name: string,
  args: Record<string, unknown> | undefined,
): MediaCardGroup | null {
  if (name !== MEDIA_CARDS_TOOL_V1 || !args) return null;
  const component = args.component;
  if (typeof component !== "string" || !COMPONENTS.has(component)) return null;
  const rawItems = Array.isArray(args.items) ? args.items : [];
  const cards: MediaCardSpec[] = [];
  const seen = new Set<string>();
  rawItems.forEach((raw, index) => {
    if (!raw || typeof raw !== "object") return;
    const spec = parseItem(component as MediaCardGroup["component"], raw as Record<string, unknown>, index);
    // 同一编号重复给出只画一次：模型偶尔会把同一部片在两处各列一遍
    if (spec && !seen.has(spec.key)) {
      seen.add(spec.key);
      cards.push(spec);
    }
  });
  if (cards.length === 0) return null;
  const title = typeof args.title === "string" && args.title.trim() ? args.title.trim() : undefined;
  return { component: component as MediaCardGroup["component"], title, cards };
}

function parseItem(
  component: MediaCardGroup["component"],
  item: Record<string, unknown>,
  index: number,
): MediaCardSpec | null {
  if (component === "library") {
    const libraryId = positiveInt(item.library_id);
    return libraryId ? { kind: "library", key: `library:${libraryId}`, libraryId } : null;
  }
  if (component === "subscription") {
    const subscriptionId = positiveInt(item.subscription_id);
    return subscriptionId
      ? { kind: "subscription", key: `subscription:${subscriptionId}`, subscriptionId }
      : null;
  }
  if (component === "title") {
    // 首选 mclaw 结果里的 title_ref；只有 TMDB 编号时按 tmdb_id + media_type 拼成同一形态
    let titleRef = typeof item.title_ref === "string" ? item.title_ref.trim() : "";
    if (!titleRef) {
      const tmdbId = positiveInt(item.tmdb_id);
      const mediaType: MediaType | null =
        item.media_type === "movie" || item.media_type === "tv" ? item.media_type : null;
      if (!tmdbId || !mediaType) return null;
      titleRef = `tmdb:${mediaType}:${tmdbId}`;
    }
    if (!TITLE_REF.test(titleRef)) return null;
    return { kind: "title", key: titleRef, titleRef };
  }
  const mediaItemId = positiveInt(item.media_item_id);
  if (!mediaItemId) return null;
  const season =
    typeof item.season_number === "number" &&
    Number.isInteger(item.season_number) &&
    item.season_number >= 0
      ? item.season_number
      : null;
  const episode = positiveInt(item.episode_number);
  // 季集必须成对；只给一半按整部处理（后端也会拒绝，这里只是回放兜底）
  const unit = season !== null && episode ? { seasonNumber: season, episodeNumber: episode } : {};
  const key = `item:${mediaItemId}${episode ? `:s${season}e${episode}` : ""}:${index}`;
  return { kind: "library_item", key, mediaItemId, ...unit };
}
