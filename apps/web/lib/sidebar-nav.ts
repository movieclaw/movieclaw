/**
 * 侧栏主导航的个人排序规则。
 *
 * 顺序存在 `ui.preferences.nav.order`（超管写全局配置域、成员写自己的
 * `member.ui_prefs`，两边由 `/ui/preferences` 按主体分流，各存各的）。
 *
 * 这里只有两个纯函数，刻意不 import 任何组件——排序规则是这个功能里唯一
 * 会出事的地方，保持无依赖才能直接单测（见 test/sidebar-nav.test.mjs）。
 *
 * 核心约定：**存下来的顺序是提示，不是契约**。导航项的全集永远以代码里的
 * 内置清单为准，保存的 id 列表只用来重排它：
 *   - 用户排过的项按保存顺序在前；
 *   - 没排过的（版本升级新增的入口）按内置默认顺序追加在后——不这么做的话，
 *     所有存过排序的老用户都再也看不到新增的导航入口，这是这类功能第一大坑；
 *   - 列表里已经不存在的 id（导航项被删、或当前权限不可见）直接忽略。
 */

/** 排序只关心 id，因此对导航项的形态只作最小约束（真实项还带 label/icon）。 */
export interface NavOrderable {
  id: string;
}

/**
 * 按保存的顺序重排导航项：排过的在前，其余按传入的内置默认顺序追加在后。
 *
 * `items` 传的是**当前用户可见**的那部分（成员没有「新会话」、无订阅权限时
 * 没有「我的订阅」），因此不可见项自然不会因为在 order 里就被排出来。
 */
export function applyNavOrder<T extends NavOrderable>(items: T[], order: string[]): T[] {
  const byId = new Map(items.map((item) => [item.id, item]));
  const ordered: T[] = [];
  for (const id of order) {
    const item = byId.get(id);
    // 已删除 / 当前不可见的 id 忽略；重复 id 只取第一次（delete 后不再命中）
    if (item) {
      ordered.push(item);
      byId.delete(id);
    }
  }
  // byId 里剩下的是"没排过的"，Map 保留插入序 = 内置默认顺序
  return [...ordered, ...byId.values()];
}

/**
 * 保存时把当前不可见项的 id 一并保留下来。
 *
 * 设置页只能列出用户此刻看得见的项，直接保存这份可见顺序会把不可见项的 id
 * 抹掉——等权限恢复（比如管理员重新给了订阅权限），那一项就变成"没排过的"。
 * 保留下来代价只有几个字符串，因此宁可留着。
 *
 * 不可见项统一追加在末尾而不是还原到原位：要精确还原就得记录它们排在谁后面，
 * 为一个"权限被收回又给回来"的边缘场景不值得。权限恢复后它出现在最后一项，
 * 用户再拖一次即可。
 */
export function mergeNavOrder(visibleOrder: string[], previousOrder: string[]): string[] {
  const visible = new Set(visibleOrder);
  return [...visibleOrder, ...previousOrder.filter((id) => !visible.has(id))];
}

/** 两份顺序是否等价（设置页判断草稿有没有改动）。 */
export function sameNavOrder(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}
