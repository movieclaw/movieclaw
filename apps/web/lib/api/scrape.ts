import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse） */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

/** 刮削与整理配置（镜像后端 MetadataScrapeSetting；空值/空列表 = 跟随环境变量）。 */
export interface ScrapeSetting {
  language_priority: string[];
  cert_country_priority: string[];
  poster_mode: "default" | "language";
  poster_language_priority: string[];
  backdrop_language_priority: string[];
  poster_min_width: number;
  backdrop_min_width: number;
  poster_size: string;
  backdrop_size: string;
  still_size: string;
  /** 命名模板；空串 = 用内置默认（即模板化之前的行为） */
  naming_entry_dir: string;
  naming_movie_file: string;
  naming_season_dir: string;
  naming_episode_file: string;
  /** 目录写入细项（库上的 write_media_assets 是总闸） */
  mirror_images: boolean;
  mirror_nfo: boolean;
  mirror_episode_thumbs: boolean;
}

/** 可按库覆盖的字段（与后端 scrape_config.LIBRARY_OVERRIDABLE 一致）。
 *  选图与语言不在其中：它们的产物跨库共享一份。 */
export const LIBRARY_OVERRIDABLE_KEYS = [
  "naming_entry_dir",
  "naming_movie_file",
  "naming_season_dir",
  "naming_episode_file",
  "mirror_images",
  "mirror_nfo",
  "mirror_episode_thumbs",
] as const;

export type LibraryOverridableKey = (typeof LIBRARY_OVERRIDABLE_KEYS)[number];

/** "跟随环境变量"字段当前的生效值（用于展示"跟随中：xxx"）。 */
export interface ScrapeEffective {
  language_priority: string[];
  cert_country_priority: string[];
  poster_size: string;
  backdrop_size: string;
  still_size: string;
}

export interface ScrapeConfigView {
  setting: ScrapeSetting;
  effective: ScrapeEffective;
}

export interface LanguageOption {
  code: string;
  name: string;
  english_name: string;
}

export interface CountryOption {
  code: string;
  name: string;
}

export function getScrapeConfig(): Promise<ScrapeConfigView> {
  return unwrap(request<ApiEnvelope<ScrapeConfigView>>("/scrape/config"));
}

export function saveScrapeConfig(payload: ScrapeSetting): Promise<ScrapeConfigView> {
  return unwrap(
    request<ApiEnvelope<ScrapeConfigView>>("/scrape/config", {
      method: "PUT",
      body: JSON.stringify(payload),
    }),
  );
}

export function listLanguageOptions(): Promise<LanguageOption[]> {
  return unwrap(request<ApiEnvelope<LanguageOption[]>>("/scrape/language-options"));
}

export function listCountryOptions(): Promise<CountryOption[]> {
  return unwrap(request<ApiEnvelope<CountryOption[]>>("/scrape/country-options"));
}

/** 发现页院线地区（页脚就地设置）。 */
export interface DiscoverRegionView {
  region: string;
  can_edit: boolean;
}

export function getDiscoverRegion(): Promise<DiscoverRegionView> {
  return unwrap(request<ApiEnvelope<DiscoverRegionView>>("/discover/region"));
}

export function setDiscoverRegion(region: string): Promise<DiscoverRegionView> {
  return unwrap(
    request<ApiEnvelope<DiscoverRegionView>>("/discover/region", {
      method: "PUT",
      body: JSON.stringify({ region }),
    }),
  );
}
