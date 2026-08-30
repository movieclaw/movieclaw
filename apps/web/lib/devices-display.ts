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

/**
 * 手工创建令牌前的权限说明。
 *
 * 与审批卡同权，但不能直接复用 `grantSummary`：那段话的收尾是「才批准」，
 * 而手工创建这条路上根本没有批准这个动作——真正要点破的是另外两件事，
 * 令牌不会自动过期，以及它一旦发出去就只能靠吊销收回。
 */
export function manualGrantSummary(): GrantSummary {
  return {
    title: "将获得：与你相同的完全权限",
    body:
      "持有这枚令牌的程序将能做你在网页上能做的一切，包括删除媒体文件。" +
      "令牌不会自动过期，只能在这里吊销——只把它放进你自己掌握的机器。",
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

// ---------------------------------------------------------------------------
// 手工令牌：给无人值守环境的环境变量片段
// ---------------------------------------------------------------------------

/** 解析出的注入地址，以及它是不是用户明确配过的。 */
export interface ServerAddress {
  url: string;
  /** true = 取自「设置 → 网络与维护」的对外访问地址；false = 拿当前浏览器地址猜的 */
  configured: boolean;
}

/**
 * 决定环境变量片段里那行地址填什么。
 *
 * 优先用「对外访问地址」——那是用户明确声明的、从网络上访问得到本应用的地址。
 * 没配时回落到当前浏览器地址，但必须把 `configured: false` 传出去让界面说破：
 * 浏览器能打开不等于目标机器连得到（NAS 的定时任务、另一个网段的 CI 都可能不通），
 * 悄悄给一个可能不通的地址，用户只会看到 mclaw 连接超时而查不到原因。
 */
export function resolveServerAddress(externalUrl: string, origin: string): ServerAddress {
  const configured = externalUrl.trim().replace(/\/+$/, "");
  if (configured) return { url: configured, configured: true };
  return { url: origin.replace(/\/+$/, ""), configured: false };
}

/**
 * 可直接粘贴的两行环境变量。
 *
 * 用 `KEY=value` 而不是 `export KEY=...`：同一份文本能同时用在 `.env`、
 * `docker --env-file`、compose 的 `env_file` 和 shell 的 `source`，覆盖面最广。
 * 顺序也不是随意的——地址在前、令牌在后，与 CLI 报错里的排查顺序一致。
 */
export function envSnippet(server: string, token: string): string {
  return `MOVIECLAW_SERVER=${server}\nMOVIECLAW_TOKEN=${token}`;
}
