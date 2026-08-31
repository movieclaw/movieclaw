/**
 * Agent 技能显式调用的前端工具（docs/design/agent-skills.md §9）。
 *
 * 两个正则都是后端 movieclaw_agent/skills.py 的 TS 镜像：
 * - SKILL_TOKEN_RE：输入框里的 /skill:名字 占位符（行首或空白后）；
 * - 技能块解析：服务端展开后的 user 消息以 <skill> 块开头，气泡把它
 *   渲染成 chip 而不是整段 XML。
 */

/** /skill:名字 占位符（maka 共享语法的镜像；构造新实例避免 lastIndex 串扰） */
export const SKILL_TOKEN_SOURCE = String.raw`(?:^|(?<=\s))\/skill:([A-Za-z0-9._-]+)`;

/** 组装一个插入输入框的占位符（尾随空格便于继续输入） */
export function skillToken(name: string): string {
  return `/skill:${name} `;
}

/** 从文本中提取占位符名字（首现序去重），供乐观轮次先渲染 chip。 */
export function parseSkillTokens(text: string): { names: string[]; text: string } {
  const names: string[] = [];
  const seen = new Set<string>();
  const stripped = text
    .split("\n")
    .map((line) => {
      let touched = false;
      const out = line.replace(new RegExp(SKILL_TOKEN_SOURCE, "g"), (_whole, name: string) => {
        touched = true;
        const key = name.toLowerCase();
        if (!seen.has(key)) {
          seen.add(key);
          names.push(name);
        }
        return "";
      });
      if (!touched) return line;
      const tidied = out.replace(/[ \t]+/g, " ").trim();
      return tidied;
    })
    .filter((line, i, all) => line.length > 0 || (i > 0 && i < all.length - 1))
    .join("\n");
  return { names, text: stripped };
}

/**
 * 把服务端已展开的 user 消息还原成 token 形态：`/skill:a /skill:b 用户原文`。
 *
 * 轮次的 input 统一存 token 形态——「改写重问」直接可编辑、重发时服务端
 * 重新展开；气泡渲染时再用 parseSkillTokens 拆成 chip + 原文。非展开
 * 消息原样返回。
 */
export function toTokenForm(text: string): string {
  const { names, text: rest } = parseSkillBlocks(text);
  if (names.length === 0) return text;
  return names.map(skillToken).join("") + rest;
}

/** 从服务端已展开的 user 消息里拆出技能名与用户原文（后端 strip_skill_blocks 镜像）。 */
export function parseSkillBlocks(text: string): { names: string[]; text: string } {
  const names: string[] = [];
  let rest = text;
  const blockRe = /^<skill name="([^"]*)" location="[^"]*">\n[\s\S]*?\n<\/skill>\n*/;
  for (;;) {
    const match = rest.match(blockRe);
    if (!match) break;
    names.push(match[1]);
    rest = rest.slice(match[0].length);
  }
  return { names, text: rest };
}
