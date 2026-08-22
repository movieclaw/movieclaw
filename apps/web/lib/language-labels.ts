/**
 * 语言标签（ISO 639-2/B 与常见私有码）→ 中文名。
 *
 * 详情页的字幕/音轨徽章与播放器的轨选择菜单共用同一份：同一条轨在两处显示
 * 成不同的名字（一处「简体中文」一处 chs），用户会以为是两条不同的轨。
 */
const LANGUAGE_LABELS: Record<string, string> = {
  chs: "简体中文",
  cht: "繁体中文",
  chi: "中文",
  zho: "中文",
  cmn: "中文",
  yue: "粤语",
  eng: "英语",
  jpn: "日语",
  kor: "韩语",
  fre: "法语",
  fra: "法语",
  ger: "德语",
  deu: "德语",
  spa: "西班牙语",
  rus: "俄语",
  ita: "意大利语",
  por: "葡萄牙语",
  tha: "泰语",
  hin: "印地语",
};

/** 未知语言（und）与空值返回 null，由调用方决定占位文案。 */
export function languageLabel(code: string | null): string | null {
  if (!code || code === "und") return null;
  return LANGUAGE_LABELS[code.toLowerCase()] ?? code;
}
