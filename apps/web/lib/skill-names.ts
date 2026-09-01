"use client";

/**
 * 已知技能名集合（小写）的共享缓存 hook。
 *
 * 气泡渲染用它判断哪些 /skill: token 是服务端真正会展开的技能（口径与
 * 后端已知名单一致，见 agent-skills.ts parseSkillTokens 的 allow 参数）。
 * 模块级缓存与 llm-thinking 同款：全站共享一次请求，失败不缓存下次重试。
 * 返回 null 表示尚未加载完成——调用方应视为「暂不过滤」而不是「无技能」，
 * 避免加载窗内 chip 闪烁成字面文本。
 */

import { useEffect, useState } from "react";

import { listSkills } from "@/lib/api/agent";

let cachedNames: Promise<ReadonlySet<string>> | null = null;

export function useSkillNames(): ReadonlySet<string> | null {
  const [names, setNames] = useState<ReadonlySet<string> | null>(null);
  useEffect(() => {
    let cancelled = false;
    cachedNames ??= listSkills()
      .then((skills) => new Set(skills.map((s) => s.name.toLowerCase())) as ReadonlySet<string>)
      .catch(() => {
        cachedNames = null; // 失败不缓存，下次挂载重试
        return new Set<string>() as ReadonlySet<string>;
      });
    void cachedNames.then((set) => {
      if (!cancelled) setNames(set);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  return names;
}
