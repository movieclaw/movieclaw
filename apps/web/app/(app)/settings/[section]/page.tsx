import type { Route } from "next";
import type { Metadata } from "next";
import { notFound, redirect } from "next/navigation";

import { SettingsPanel } from "@/components/settings-view";
import { settingsSections } from "@/lib/mock-data";

export async function generateMetadata({
  params,
}: {
  params: Promise<{ section: string }>;
}): Promise<Metadata> {
  const { section } = await params;
  const label = settingsSections.find((s) => s.id === section)?.label;
  return { title: label ? `${label} · 设置` : "设置" };
}

/** 设置分区（/settings/[section]）：概览 / 个人信息 / 外观 / 站点 / 下载器 等。 */
export default async function SettingsSectionPage({
  params,
  searchParams,
}: {
  params: Promise<{ section: string }>;
  searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { section } = await params;
  // 旧「搜索」分区已并入「资源站点」，老书签/历史链接重定向过去，不要 404
  if (section === "search") redirect("/settings/sites" as Route);
  // 旧「关于与更新」分区已并入「应用」（现「更新与维护」），同样重定向兜底
  if (section === "about") redirect("/settings/app" as Route);
  // 旧「应用 → 远程转码」标签已升级为「播放」分区，带 tab=remote 的老链接跟过去
  if (section === "app" && (await searchParams).tab === "remote") {
    redirect("/settings/playback" as Route);
  }
  if (!settingsSections.some((s) => s.id === section)) notFound();
  return <SettingsPanel active={section} />;
}
