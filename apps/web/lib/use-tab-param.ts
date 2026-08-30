"use client";

import { useCallback, useEffect, useState } from "react";

/**
 * 设置分区内胶囊标签的 ?tab= 深链支持：
 *   - 挂载时读取地址栏的 tab 参数（合法值才采用），/settings/xxx?tab=yyy 可以
 *     直达某个标签——引导链接、通知里的跳转都能把人送到确切位置；
 *   - 切换标签时用原生 history.replaceState 把参数写回地址栏（切回默认标签则
 *     清掉），刷新/收藏/分享当前页不再丢失所在标签。用 replaceState 而不是
 *     router.replace：不触发 Next 重新渲染与滚动重置，也不产生历史记录
 *     （切标签不该把返回键变成"退回上一个标签"）。Next App Router 支持原生
 *     history API 同步地址栏，前提是保留 history.state 原样传回。
 *
 * 读取放在 useEffect 而非 useState 初始值：服务端渲染阶段没有 window，
 * 初始值读取会造成 SSR 与客户端首帧不一致。
 */
export function useTabParam<T extends string>(
  validTabs: readonly T[],
  defaultTab: T,
): [T, (next: T) => void] {
  const [tab, setTab] = useState<T>(defaultTab);

  useEffect(() => {
    const requested = new URLSearchParams(window.location.search).get("tab");
    if (requested && (validTabs as readonly string[]).includes(requested)) {
      setTab(requested as T);
    }
    // validTabs 是调用方的字面量常量；地址栏只在挂载时读一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const select = useCallback(
    (next: T) => {
      setTab(next);
      const url = new URL(window.location.href);
      if (next === defaultTab) url.searchParams.delete("tab");
      else url.searchParams.set("tab", next);
      window.history.replaceState(window.history.state, "", url);
    },
    [defaultTab],
  );

  return [tab, select];
}
