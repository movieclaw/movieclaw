import type { MediaLibrary } from "./api/libraries";

/** 规则字段 → 界面维度名（v1 仅两个维度），用于重叠提示里点名该补哪个条件。 */
const RULE_FIELD_LABELS: Record<string, string> = {
  genres: "类型",
  origin_countries: "区域",
};

/**
 * 同类型两库的收藏范围可能同时命中同一部作品、且特异性（条件条数）相同
 * ——命中顺序只能靠创建先后，给只读提示（不阻断：加一个条件即可消解）。
 * 「可能同时命中」= 两库共同声明的每个字段取值都有交集。
 * 提示不止陈述事实，还给出具体做法：点名给创建更晚（即将吃亏）的那个库
 * 补上它缺的维度——条件数多者优先命中，补完歧义即消。
 */
export function routingOverlapWarnings(libraries: MediaLibrary[]): string[] {
  const declared = libraries.filter((l) => l.match_rules.length > 0);
  const warnings: string[] = [];
  for (let i = 0; i < declared.length; i++) {
    for (let j = i + 1; j < declared.length; j++) {
      const a = declared[i];
      const b = declared[j];
      if (a.kind !== b.kind || a.match_rules.length !== b.match_rules.length) continue;
      const compatible = a.match_rules.every((ra) => {
        const rb = b.match_rules.find((r) => r.field === ra.field);
        if (!rb) return true; // 字段只在一边：不妨碍同时命中
        return ra.values.some((v) => rb.values.includes(v));
      });
      if (compatible) {
        const [first, later] = (a.id ?? 0) < (b.id ?? 0) ? [a, b] : [b, a];
        const laterFields = new Set<string>(later.match_rules.map((r) => r.field));
        const missing = Object.entries(RULE_FIELD_LABELS)
          .filter(([field]) => !laterFields.has(field))
          .map(([, label]) => label);
        const fix =
          missing.length > 0
            ? `想让「${later.name}」优先收这类作品：编辑它，补上「${missing.join("」或「")}」条件` +
              `（用「全选」也可以）——条件多的库优先命中；想维持现状则无需改动。`
            : `两库已声明相同的维度：错开重叠的取值即可消除歧义。`;
        warnings.push(
          `「${a.name}」与「${b.name}」的收藏范围可能同时命中同一部作品且条件数相同，` +
            `届时优先进创建更早的「${first.name}」。${fix}`,
        );
      }
    }
  }
  return warnings;
}
