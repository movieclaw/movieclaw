import { redirect } from "next/navigation";
import type { Route } from "next";

import { settingsSections } from "@/lib/mock-data";

/**
 * /settings 裸地址重定向到首个分区，保证设置页始终有明确的分区地址。
 * 首个分区即「概览」落地页——管理员进设置先看到配置状态与下一步；
 * 成员没有概览（见 MEMBER_SECTION_IDS），SettingsPanel 会兜底到个人信息。
 */
export default function SettingsIndexPage() {
  redirect(`/settings/${settingsSections[0].id}` as Route);
}
