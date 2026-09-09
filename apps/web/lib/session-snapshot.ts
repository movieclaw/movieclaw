/**
 * 会话快照的通用存放处：一个带上限的「最近用过的留下」小仓库。
 *
 * 为什么全站需要它：列表页与详情页是两个路由，Next 切走时列表组件会被卸载。
 * 返回时若从零重新加载，已加载的分页窗口就没了——容器矮到装不下离开时的滚动
 * 位置，滚动恢复（lib/use-scroll-restoration.ts）找不到落脚点，人被甩回列表首。
 * 所以每一面墙都要把「离开时的那一屏」留在内存里，返回时先接回来再与服务端对账。
 *
 * 这件事本身各页都一样，只有存什么不一样，于是收成这一份：调用方给出上限和
 * 自己的快照类型，拿到 get / set 两个口子。写入时把这一条移到队尾，超限时淘汰
 * 最久没写过的那一条——用户在几十个列表间来回导航时条目不会无限增长。
 *
 * **只活在当前浏览会话里**（模块级内存，不写 localStorage）：刷新页面即失效。
 * 位置与窗口都属于一次导航上下文，刷新后从列表顶部重新开始才符合预期；跨会话
 * 的位置记忆是另一件事，走 lib/library-wall-recall.ts 的胶囊问一句。
 *
 * 纯逻辑模块，`node --test` 直接跑。
 */

export interface SessionSnapshots<K, V> {
  /** 没存过（或已被淘汰）时返回 undefined，调用方据此走完整加载。 */
  get(key: K): V | undefined;
  set(key: K, value: V): void;
  /** 当前存着几条（测试与调试用）。 */
  readonly size: number;
}

/**
 * 建一个上限为 `capacity` 的快照仓库。
 *
 * key 用什么由调用方定：单库页用库 id，片单页用片单引用，滚动位置用列表键。
 * 同一个 key 重复写入是覆盖，并把它当作「刚用过」。
 */
export function createSessionSnapshots<K, V>(capacity: number): SessionSnapshots<K, V> {
  const entries = new Map<K, V>();
  return {
    get: (key) => entries.get(key),
    set(key, value) {
      // 先删再插：Map 按插入顺序迭代，这样队首恒是最久没写过的那一条
      entries.delete(key);
      entries.set(key, value);
      while (entries.size > capacity) {
        const oldest = entries.keys().next().value;
        if (oldest === undefined) break;
        entries.delete(oldest);
      }
    },
    get size() {
      return entries.size;
    },
  };
}
