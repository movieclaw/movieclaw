/**
 * 刷流清理的文案（任务中心刷流分组、站点设置的关闭刷流 / 删除站点三处共用，
 * iOS 同名 `ActivityBoostCleanupText`）。机制见 docs/design/site-protection-ratio-boost.md §2.9。
 */
import dayjs from "dayjs";

import type { BoostCleanupResult } from "@/lib/api/sites";
import { formatBytes } from "@/lib/format";

/** 保留期到期时刻「9月29日 14:00」 */
export function formatCleanupDeadline(iso: string | null | undefined): string | null {
  return iso ? dayjs(iso).format("M月D日 HH:mm") : null;
}

/** 清理结果的一句话回执 */
export function boostCleanupSummary(result: BoostCleanupResult): string {
  const parts: string[] = [];
  if (result.deleted_count > 0) {
    parts.push(`已删除 ${result.deleted_count} 个刷流种子，释放 ${formatBytes(result.deleted_bytes)}`);
  }
  if (result.scheduled_count > 0) {
    const until = formatCleanupDeadline(result.scheduled_until);
    parts.push(`${result.scheduled_count} 个还在保留期内，到期后自动删除${until ? `（最晚 ${until}）` : ""}`);
  }
  if (result.failed_count > 0) {
    parts.push(`${result.failed_count} 个因下载器暂时连不上没删成，稍后自动重试`);
  }
  return parts.length > 0 ? parts.join("；") : "没有需要清理的刷流种子";
}

/** 关闭刷流 / 删除站点确认框里的「同时清理」勾选项（该站还有在池种子时才出现） */
export function boostCleanupCheckbox(count: number, bytes: number) {
  return {
    label: `同时清理该站的 ${count} 个刷流种子（${formatBytes(bytes)}）`,
    description:
      "连数据文件一起删除，无法恢复；还没做满站点要求做种时长的，等到期后再自动删，避免被记 H&R。不勾选则种子继续做种，之后可在「活动 → 任务 → 刷流做种」清理。",
    defaultChecked: false,
  };
}
