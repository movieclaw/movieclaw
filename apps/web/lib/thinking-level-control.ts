/**
 * 思维链强度控件的纯逻辑（无 React、无 API 依赖，供 node --test 直接导入）。
 *
 * 控件本身住在模型菜单的下半（见 composer.tsx 的 ModelMenu）：服务端只下发
 * 一个字符串菜单（thinking_levels），前端不理解方言，但要按菜单形状决定
 * 下半长什么样：
 *   - 空菜单            → 不渲染（该模型强度不可控）
 *   - 只有一档          → 两格分段（默认 / 该档）。toggle 方言（kimi-k2.6、
 *                         glm-5.x）的菜单就是单项「关」，画成一根只有一个刻度
 *                         的滑杆既没有可拖的距离，点了刻度也再点不回默认
 *   - 两档及以上        → 横向离散滑杆，刻度按统一词汇表排序
 *
 * 「默认」= 不发任何参数、用模型自身行为，不是强度轴上的一点，所以滑杆上
 * 不占刻度（默认态无滑块），靠「恢复默认」回去；分段里则是普通一格。
 */

/** 统一词汇表的强度顺序（越靠后越深）：滑杆按它排刻度，服务端下发的菜单也按它归一。 */
export const THINKING_LEVEL_ORDER = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

/** 档位文案（对齐 maka 的短单词标签）；「默认」由选择器的空值表达。 */
export const THINKING_LEVEL_LABELS: Record<string, string> = {
  off: "关",
  minimal: "最少",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "超高",
  max: "最高",
};

export type ThinkingControlShape = "hidden" | "toggle" | "slider";

/** 服务端菜单 → 按词汇表排序的刻度；词汇表外的值丢弃（声明不是有序集合）。 */
export function thinkingStops(levels: readonly string[]): string[] {
  return THINKING_LEVEL_ORDER.filter((level) => levels.includes(level));
}

/** 按刻度数决定控件形态（见文件头注释）。 */
export function thinkingControlShape(stops: readonly string[]): ThinkingControlShape {
  if (stops.length === 0) return "hidden";
  return stops.length === 1 ? "toggle" : "slider";
}

/** 刻度圆心在轨道可用长度上的位置（0..100）：刻度均布，只有一个时居中。 */
export function stopPercent(index: number, count: number): number {
  return count > 1 ? (index / (count - 1)) * 100 : 50;
}

/** 轨道可用长度上 0..1 的位置 → 最近的刻度下标；越界钳到两端。 */
export function nearestStopIndex(ratio: number, count: number): number {
  if (count <= 1) return 0;
  const clamped = Math.min(1, Math.max(0, ratio));
  return Math.round(clamped * (count - 1));
}

/**
 * 指针横坐标 → 刻度下标。轨道两端各留 inset 给刻度圆心（圆心不能贴边，
 * 否则滑块会溢出轨道），可用长度 = 轨道宽 − 2·inset；点在留白上按端点算。
 */
export function stopIndexAtPointer(
  clientX: number,
  trackLeft: number,
  trackWidth: number,
  count: number,
  inset: number,
): number {
  const usable = trackWidth - inset * 2;
  if (usable <= 0) return 0;
  return nearestStopIndex((clientX - trackLeft - inset) / usable, count);
}

/**
 * 键盘步进：←/→（↓/↑）相邻刻度，Home/End 两端；默认态（index=-1）按任一
 * 方向都先落到最浅一档。不是导航键返回 null（调用方不拦截该按键）。
 */
export function steppedStopIndex(index: number, count: number, key: string): number | null {
  if (count === 0) return null;
  const last = count - 1;
  switch (key) {
    case "ArrowRight":
    case "ArrowUp":
      return Math.min(index + 1, last);
    case "ArrowLeft":
    case "ArrowDown":
      return Math.max(index - 1, 0);
    case "Home":
      return 0;
    case "End":
      return last;
    default:
      return null;
  }
}

/** 分段形态的一格：level=null 即「默认」（不发参数）。 */
export interface ThinkingListItem {
  level: string | null;
  label: string;
  /** 分段下方的灰字：说明选中这一格到底发什么、后果是什么 */
  description: string;
}

/** 单档菜单是否就是 toggle 方言的「只能关」（kimi-k2.6、glm-5.x）。 */
function isOffOnly(stops: readonly string[]): boolean {
  return stops.length === 1 && stops[0] === "off";
}

/**
 * 分段形态的两格文案。只有「关」的模型，「默认」对用户没有信息量——它到底
 * 是开还是关？按后端定义（toggle：开 = 默认，不发参数），直接写成
 * 「开启（模型默认）/ 关闭」，各配一句说明（菜单里只显示当前选中那句）。
 * 单档不是「关」的罕见声明（比如只声明了 max 的自定义模型）退回通用的
 * 「默认 / 该档」。
 */
export function thinkingListItems(stops: readonly string[]): ThinkingListItem[] {
  if (isOffOnly(stops)) {
    return [
      {
        level: null,
        label: "开启（模型默认）",
        description: "不发送思考参数，沿用模型自身的默认行为",
      },
      {
        level: "off",
        label: "关闭",
        description: "发送关闭指令，不再输出思考过程，响应更快",
      },
    ];
  }
  return [
    { level: null, label: "默认", description: "不发送思考参数，由模型自行决定强度" },
    ...stops.map((level) => ({
      level,
      label: THINKING_LEVEL_LABELS[level] ?? level,
      description: `按「${THINKING_LEVEL_LABELS[level] ?? level}」强度思考`,
    })),
  ];
}

/** 滑杆标题上的当前值文案：未选档位就是「默认」（由模型自行决定）。 */
export function thinkingValueLabel(value: string | null): string {
  return value === null ? "默认" : (THINKING_LEVEL_LABELS[value] ?? value);
}
