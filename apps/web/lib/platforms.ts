/** 规则组与搜索结果共用的流媒体平台展示目录；value 与后端规范值严格一致。 */
export interface PlatformOption {
  value: string;
  label: string;
  aliases: string;
  group: "国际" | "亚洲" | "动漫";
}

export const PLATFORM_OPTIONS: PlatformOption[] = [
  { value: "amazon", label: "Amazon Prime Video", aliases: "AMZN Amazon", group: "国际" },
  { value: "apple_tv", label: "Apple TV", aliases: "ATV", group: "国际" },
  { value: "apple_tv_plus", label: "Apple TV+", aliases: "ATVP APTV", group: "国际" },
  { value: "netflix", label: "Netflix", aliases: "NF", group: "国际" },
  { value: "disney_plus", label: "Disney+", aliases: "DSNP DSNY", group: "国际" },
  { value: "hbo", label: "HBO", aliases: "HBO", group: "国际" },
  { value: "hbo_max", label: "HBO Max", aliases: "HMAX HBOM", group: "国际" },
  { value: "max", label: "Max", aliases: "MAX", group: "国际" },
  { value: "hulu", label: "Hulu", aliases: "HULU", group: "国际" },
  { value: "paramount_plus", label: "Paramount+", aliases: "PMTP", group: "国际" },
  { value: "peacock", label: "Peacock", aliases: "PCOK", group: "国际" },
  { value: "now", label: "NOW / Sky", aliases: "NOW", group: "国际" },
  { value: "showtime", label: "Showtime", aliases: "SHO", group: "国际" },
  { value: "discovery_plus", label: "Discovery+", aliases: "DSCP DCP DISC DSCV+", group: "国际" },
  { value: "stan", label: "Stan", aliases: "STAN", group: "国际" },
  { value: "crave", label: "Crave", aliases: "CRAV CRAVE", group: "国际" },
  { value: "roku", label: "Roku Channel", aliases: "ROKU", group: "国际" },
  { value: "google_tv", label: "Google TV", aliases: "PLAY", group: "国际" },
  { value: "itunes", label: "iTunes", aliases: "iT", group: "国际" },
  { value: "sony_core", label: "Sony Pictures Core", aliases: "BCORE CORE", group: "国际" },
  { value: "criterion", label: "Criterion Channel", aliases: "CRiT", group: "国际" },
  { value: "iqiyi", label: "iQIYI / 爱奇艺", aliases: "IQ IQIY", group: "亚洲" },
  { value: "wetv", label: "Tencent Video / WeTV", aliases: "WETV", group: "亚洲" },
  { value: "youku", label: "Youku / 优酷", aliases: "YOUKU", group: "亚洲" },
  { value: "mangotv", label: "MangoTV / 芒果 TV", aliases: "MGTV", group: "亚洲" },
  { value: "bilibili", label: "Bilibili", aliases: "BILI", group: "亚洲" },
  { value: "viu", label: "Viu", aliases: "VIU", group: "亚洲" },
  { value: "nowplayer", label: "Now Player", aliases: "NowPlayer", group: "亚洲" },
  { value: "mytv_super", label: "myTV SUPER", aliases: "MyTVSuper", group: "亚洲" },
  { value: "myvideo", label: "MyVideo", aliases: "MyVideo", group: "亚洲" },
  { value: "hami_video", label: "Hami Video", aliases: "HamiVideo", group: "亚洲" },
  { value: "line_tv", label: "LINE TV", aliases: "LINETV", group: "亚洲" },
  { value: "friday", label: "friDay Video", aliases: "friDay", group: "亚洲" },
  { value: "kktv", label: "KKTV", aliases: "KKTV", group: "亚洲" },
  { value: "tving", label: "TVING", aliases: "TVING", group: "亚洲" },
  { value: "wavve", label: "Wavve", aliases: "WAVVE", group: "亚洲" },
  { value: "coupang_play", label: "Coupang Play", aliases: "CPNG", group: "亚洲" },
  { value: "kocowa", label: "KOCOWA", aliases: "KCW", group: "亚洲" },
  { value: "viki", label: "Rakuten Viki", aliases: "Viki", group: "亚洲" },
  { value: "unext", label: "U-NEXT", aliases: "U-NEXT", group: "亚洲" },
  { value: "tver", label: "TVer", aliases: "TVer", group: "亚洲" },
  { value: "fod", label: "FOD", aliases: "FOD", group: "亚洲" },
  { value: "dmm_tv", label: "DMM TV", aliases: "DMM-TV", group: "亚洲" },
  { value: "hotstar", label: "Hotstar", aliases: "HTSR DSNPHS", group: "亚洲" },
  { value: "crunchyroll", label: "Crunchyroll", aliases: "CR", group: "动漫" },
  { value: "hidive", label: "HIDIVE", aliases: "HIDI", group: "动漫" },
  { value: "abema", label: "ABEMA", aliases: "ABEMA TV", group: "动漫" },
  { value: "adn", label: "Animation Digital Network", aliases: "ADN", group: "动漫" },
  { value: "funimation", label: "Funimation", aliases: "FUNi", group: "动漫" },
  { value: "vrv", label: "VRV", aliases: "VRV", group: "动漫" },
  { value: "wakanim", label: "Wakanim", aliases: "WKN", group: "动漫" },
  { value: "b_global", label: "Bilibili Global", aliases: "B-Global", group: "动漫" },
];

const PLATFORM_LABELS = new Map(PLATFORM_OPTIONS.map((option) => [option.value, option.label]));

export function platformLabel(value: string): string {
  return PLATFORM_LABELS.get(value) ?? value;
}
