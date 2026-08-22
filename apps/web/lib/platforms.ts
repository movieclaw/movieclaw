/**
 * 流媒体平台的展示层：规范值 → 中文/惯用展示名。
 *
 * 规范值（netflix / disney_plus / …）是后端契约，由 `movieclaw_enrich/vocab.py`
 * 的 `PLATFORM` 词表定义；这里只负责好看。表里没有的值**原样展示**——词表演进
 * 时新增的平台不至于在界面上变成空白，与后端"未知值丢弃而非报错"的宽容取向一致。
 */

export const PLATFORM_LABELS: Record<string, string> = {
  // 国际
  netflix: "Netflix",
  amazon: "Prime Video",
  disney_plus: "Disney+",
  hbo_max: "HBO Max",
  hulu: "Hulu",
  apple_tv_plus: "Apple TV+",
  paramount_plus: "Paramount+",
  peacock: "Peacock",
  showtime: "Showtime",
  starz: "Starz",
  discovery_plus: "Discovery+",
  crave: "Crave",
  stan: "Stan",
  roku: "Roku",
  google_tv: "Google TV",
  itunes: "iTunes",
  sony_core: "Sony Pictures Core",
  criterion: "Criterion",
  // 中国及亚洲
  iqiyi: "爱奇艺",
  wetv: "腾讯视频",
  youku: "优酷",
  mangotv: "芒果 TV",
  bilibili: "哔哩哔哩",
  viu: "Viu",
  nowplayer: "Now player",
  mytv_super: "myTV SUPER",
  hami_video: "Hami Video",
  line_tv: "LINE TV",
  kktv: "KKTV",
  tving: "TVING",
  wavve: "Wavve",
  coupang_play: "Coupang Play",
  kocowa: "KOCOWA",
  viki: "Viki",
  unext: "U-NEXT",
  tver: "TVer",
  fod: "FOD",
  dmm_tv: "DMM TV",
  hotstar: "Hotstar",
  // 动漫
  crunchyroll: "Crunchyroll",
  hidive: "HIDIVE",
  abema: "ABEMA",
  adn: "ADN",
  funimation: "Funimation",
  vrv: "VRV",
  wakanim: "Wakanim",
  b_global: "B-Global",
};

/** 规范值 → 展示名；未知值原样返回。 */
export function platformLabel(id: string): string {
  return PLATFORM_LABELS[id] ?? id;
}

/**
 * 规则组编辑器里常驻的平台选项——覆盖绝大多数中英文资源的来源。
 * 其余平台仍可经 API 配置，编辑器会原样保留（不会因为没上榜而被存盘时丢掉）。
 */
export const PLATFORM_OPTIONS: string[] = [
  "netflix",
  "disney_plus",
  "amazon",
  "hbo_max",
  "apple_tv_plus",
  "hulu",
  "paramount_plus",
  "peacock",
  "iqiyi",
  "wetv",
  "youku",
  "mangotv",
  "bilibili",
  "viu",
  "crunchyroll",
  "hotstar",
];
