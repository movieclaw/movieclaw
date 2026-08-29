/**
 * 「设备」分区的展示口径（docs/design/device-auth.md §7）。
 *
 * 单独成文件而不是留在组件里，是因为这里的措辞是**安全设计的一部分**：
 * v1 除转码 Worker 外的令牌都是完全权限，用户批准时看到的那行「将获得」
 * 就是唯一的一道人工闸（§4.5）。措辞退化成含糊的技术名词，闸就没了——
 * 所以它需要被测试锁住，而不是散落在 JSX 里随手改。
 */

/** 客户端形态 → 给人看的说法。内部值不上屏。 */
const CLIENT_TYPE_LABEL: Record<string, string> = {
  worker: "转码 Worker",
  cli: "命令行 / Agent",
  manual: "手工令牌",
};

export function clientTypeLabel(type: string): string {
  return CLIENT_TYPE_LABEL[type] ?? "未知类型";
}

export interface GrantSummary {
  title: string;
  body: string;
}

/**
 * 批准前的权限说明。两条硬要求：
 * - 说人话，不出现 scope 之类的内部名词；
 * - 命令行那条必须把全权的含义点破到具体后果（删除媒体文件），
 *   因为 v1 没有别的收窄手段。
 */
export function grantSummary(type: string): GrantSummary {
  if (type === "worker") {
    return {
      title: "将获得：仅限转码",
      body: "这台机器不能查看或修改你的订阅、媒体库和设置。",
    };
  }
  return {
    title: "将获得：与你相同的完全权限",
    body:
      "这台机器上的程序将能做你在网页上能做的一切，包括删除媒体文件。" +
      "只在你清楚这台机器上正在运行什么程序时才批准。",
  };
}

/** 已连接列表里的权限标注。同样是实话，不是内部权限名。 */
export function grantBadge(type: string): string {
  return type === "worker" ? "仅转码" : "完全权限";
}

/**
 * 「刚刚活跃 / 12 分钟前 / 3 天前」。
 *
 * 吊销是这套设计唯一的事后止损手段，用户靠这一列判断「哪台还在用、哪台可以
 * 关掉」，所以宁可粗一点也要读得懂——令牌的活跃时间本身就按分钟粒度落盘，
 * 再精确没有意义。
 */
export function relativeTime(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "从未使用";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "未知";
  const minutes = Math.floor((now - then) / 60000);
  if (minutes < 1) return "刚刚活跃";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return `${Math.floor(hours / 24)} 天前`;
}

/** 最近 5 分钟内用过就算在线——落盘粒度是分钟，阈值再小就是假精度。 */
export function isLive(iso: string | null, now: number = Date.now()): boolean {
  if (!iso) return false;
  const then = Date.parse(iso);
  return !Number.isNaN(then) && now - then < 5 * 60 * 1000;
}
