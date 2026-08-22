/**
 * 进度条缩略图定位（docs/design/web-player.md §6.5）。
 *
 * 服务端把整片按固定间隔抽帧拼成雪碧图，只给一份索引；「第 t 秒该显示哪张图的
 * 哪一格」由前端算。索引是 JSON 而不是 WebVTT——雪碧图地址带签名 token，
 * VTT 里的相对路径解析会把 query 丢掉，浏览器请求时没有凭据。
 *
 * 这几行数学单独成模块是为了能测：算错一格，预览图就会错位一条边，或者指到
 * 一张不存在的雪碧图上（表现为悬停时一片空白）。
 */

export interface TrickplayIndex {
  ready: boolean;
  interval_ms: number;
  tile_width: number;
  tile_height: number;
  columns: number;
  rows: number;
  /** 有效缩略图张数——最后一张雪碧图通常没填满，超出的格子是黑的 */
  count: number;
  sheets: string[];
}

export interface TrickplayTile {
  /** 雪碧图地址（已带 token） */
  url: string;
  width: number;
  height: number;
  /** CSS background-position：负值把目标格子挪进视口 */
  offsetX: number;
  offsetY: number;
}

/**
 * 文件时间（毫秒）→ 该显示的那一格；索引没就绪返回 null。
 *
 * 越界一律夹到最后一格而不是返回 null：拖到片尾时给最后一帧，比忽然没有预览
 * 好——预览时有时无，用户会以为播放器坏了。
 */
export function tileAt(index: TrickplayIndex | null, fileMs: number): TrickplayTile | null {
  if (!index?.ready || index.count <= 0 || index.interval_ms <= 0) return null;
  if (index.columns <= 0 || index.rows <= 0 || index.sheets.length === 0) return null;

  const tilesPerSheet = index.columns * index.rows;
  const ordinal = Math.min(
    Math.floor(Math.max(0, fileMs) / index.interval_ms),
    index.count - 1,
  );
  const sheetIndex = Math.min(
    Math.floor(ordinal / tilesPerSheet),
    index.sheets.length - 1,
  );
  const withinSheet = ordinal - sheetIndex * tilesPerSheet;
  return {
    url: index.sheets[sheetIndex],
    width: index.tile_width,
    height: index.tile_height,
    offsetX: -(withinSheet % index.columns) * index.tile_width,
    offsetY: -Math.floor(withinSheet / index.columns) * index.tile_height,
  };
}
