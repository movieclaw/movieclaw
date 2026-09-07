import type {
  LibraryGalleryGroup,
  LibraryIndexEntry,
  LibraryItem,
  LibraryItemSort,
  MediaLibrary,
  MetadataRefreshProgress,
  MissingItem,
  ReviewGroup,
  UnidentifiedGroup,
} from "@/lib/api/libraries";
import type { Subscription } from "@/lib/api/subscriptions";

/**
 * 单库页离开再返回时的会话快照。
 *
 * 媒体详情与库存墙是两个路由，Next 切换时会卸载库存墙组件。快照保留
 * 已加载的分页窗口，让返回时先恢复原来的视觉位置，再在后台与服务端对账。
 */
export interface LibraryDetailSnapshot {
  libraries: MediaLibrary[];
  items: LibraryItem[];
  wallHasMore: boolean;
  ownedIds: Set<number>;
  wallIndex: LibraryIndexEntry[];
  wallStart: number;
  unidentified: UnidentifiedGroup[];
  review: ReviewGroup[];
  ignored: UnidentifiedGroup[];
  missing: MissingItem[];
  subscriptions: Subscription[];
  metaRefresh: MetadataRefreshProgress | null;
  wallLoaded: number;
  wallSort: LibraryItemSort;
  wallOffset: number;
  /**
   * 图床浏览模式已加载的整份窗口。它必须跟着快照一起回来：图廊只按条目分页，
   * 只补第一页的话容器会矮到装不下离开时的滚动位置，人就被甩回墙首。
   * 空数组 = 这一次浏览没进过图廊。
   */
  galleryGroups: LibraryGalleryGroup[];
  galleryHasMore: boolean;
  /** 已向服务端请求到第几个条目（图廊按条目分页），返回后按同样的页数对账 */
  galleryLoaded: number;
  /** 删除或转移等操作后标记为过期；保留旧窗口只为让滚动恢复有落脚点。 */
  stale: boolean;
}

const MAX_LIBRARY_DETAIL_SNAPSHOTS = 20;
const snapshots = new Map<number, LibraryDetailSnapshot>();

export function getLibraryDetailSnapshot(libraryId: number) {
  return snapshots.get(libraryId);
}

export function setLibraryDetailSnapshot(
  libraryId: number,
  snapshot: LibraryDetailSnapshot,
) {
  // 重新写入时移到 Map 尾部，超限时优先淘汰最久未更新的库。
  snapshots.delete(libraryId);
  snapshots.set(libraryId, snapshot);
  while (snapshots.size > MAX_LIBRARY_DETAIL_SNAPSHOTS) {
    const oldest = snapshots.keys().next().value;
    if (oldest === undefined) break;
    snapshots.delete(oldest);
  }
}

/**
 * 让返回后的页面保留旧窗口但必须重新向服务端对账。
 * 没有快照时无需额外记录：首次进入本来就会执行完整加载。
 */
export function invalidateLibraryDetailSnapshot(libraryId: number) {
  const snapshot = snapshots.get(libraryId);
  if (!snapshot || snapshot.stale) return;
  setLibraryDetailSnapshot(libraryId, { ...snapshot, stale: true });
}
