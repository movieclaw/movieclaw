import type { ConfirmOptions } from "@/components/feedback";

/**
 * 扫描库 / 刷新元数据的二次确认文案——三处入口共用一份，口径一致：
 * 首页库卡片菜单、库详情 ⋯ 菜单、条目详情工具栏。
 *
 * 写法约定（2026-08-04 用户反馈）：短导语 + 动作清单（bullets），
 * 用户语言、不用行话（"台账/刮削/介质规格"一律翻译成人话）；
 * 每份文案最后一条讲安全边界（会不会动我的文件）。只有「开始」需要
 * 确认；进行中的「停止」不设确认——停止本身就是在纠正一个重操作。
 */

export function scanLibraryConfirm(name: string): ConfirmOptions {
  return {
    title: `扫描「${name}」？`,
    description: "本次扫描会检查库文件夹的最新变化：",
    bullets: [
      "找出新增的影片文件，自动识别并加入媒体库",
      "标记已经不在硬盘上的文件（记录保留，文件回来自动恢复）",
      "为新入库的影片补齐简介、海报等信息",
      "首次扫描会读取每个文件的画质与音轨信息，文件多时较慢、可随时停止",
      "不会移动、修改或删除你的任何文件",
    ],
    confirmLabel: "开始扫描",
  };
}

export function refreshLibraryConfirm(name: string): ConfirmOptions {
  return {
    title: `刷新「${name}」的元数据？`,
    description: "本次刷新会为库里的全部影片：",
    bullets: [
      "重新获取最新的简介、评分、演职员和剧集信息",
      "海报或背景图有更新时重新下载（你手动锁定的不动）",
      "同步更新影片文件夹里的海报和 NFO 信息文件",
      "影片较多时需要一段时间，可随时停止",
      "不会移动、修改或删除你的视频文件",
    ],
    confirmLabel: "开始刷新",
  };
}

export function refreshItemConfirm(title: string): ConfirmOptions {
  return {
    title: `刷新《${title}》的元数据？`,
    description: "本次刷新会：",
    bullets: [
      "重新获取这部作品的简介、评分和演职员",
      "重新下载海报和背景图（你手动锁定的不动）",
      "同步更新影片文件夹里的海报和 NFO 信息文件",
      "不会改动你的视频文件",
    ],
    confirmLabel: "刷新",
  };
}
