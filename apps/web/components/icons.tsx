/**
 * 项目内联图标集。
 * 统一用 24x24、描边风格的 SVG，避免为骨架引入额外图标库（保持依赖精简）。
 * 颜色继承 currentColor，尺寸由外部通过 className（如 size-4）控制。
 */
import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function Base({ children, ...props }: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...props}
    >
      {children}
    </svg>
  );
}

export const PlusIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M12 5v14M5 12h14" />
  </Base>
);

export const SparkIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z" />
  </Base>
);

/** 魔法棒：业界通用的「AI 生成」手势，用在会产出新内容的入口上（区别于
 *  SparkIcon 那种表示「AI 能力/模型」的星芒）。 */
export const WandIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M4 20 14.5 9.5" />
    <path d="m17.5 6.5-2 2-2-2 2-2 2 2Z" />
    <path d="M18 13v3M16.5 14.5h3M6.5 3v3M5 4.5h3" />
  </Base>
);

export const PuzzleIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M10.5 4a1.5 1.5 0 0 1 3 0V5h3.5a1 1 0 0 1 1 1v3.5h1a1.5 1.5 0 0 1 0 3h-1V16a1 1 0 0 1-1 1h-3.5v1a1.5 1.5 0 0 1-3 0v-1H7a1 1 0 0 1-1-1v-3.5H5a1.5 1.5 0 0 1 0-3h1V6a1 1 0 0 1 1-1h3.5V4Z" />
  </Base>
);

export const BookmarkIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M6 4.5a1 1 0 0 1 1-1h10a1 1 0 0 1 1 1V20l-6-3.5L6 20V4.5Z" />
  </Base>
);

export const CopyIcon = (p: IconProps) => (
  <Base {...p}>
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h8" />
  </Base>
);

export const CheckIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M5 12.5l4 4 10-10" />
  </Base>
);

export const XIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M6 6l12 12M18 6L6 18" />
  </Base>
);

export const FilmIcon = (p: IconProps) => (
  <Base {...p}>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M7 4v16M17 4v16M3 9h4M3 15h4M17 9h4M17 15h4" />
  </Base>
);

export const TvIcon = (p: IconProps) => (
  <Base {...p}>
    <rect x="3" y="6" width="18" height="12" rx="2" />
    <path d="M8 21h8M12 3v3" />
  </Base>
);

export const ClockIcon = (p: IconProps) => (
  <Base {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 7.5V12l3 1.8" />
  </Base>
);

/**
 * 历史：时钟 + 逆时针回拨箭头。
 * 用于「最近观看」这类"已经发生过"的记录，与表达"此刻正在发生"的
 * PlayIcon 区分开——两者都用播放三角时，用户无法一眼分辨实时与回顾。
 */
export const HistoryIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M3.5 12a8.5 8.5 0 1 0 2.6-6.1L3.5 8.3" />
    <path d="M3.5 3.6v4.7h4.7" />
    <path d="M12 7.5V12l3 1.8" />
  </Base>
);

/** 活动：脉冲折线。时钟表达的是"排队与历史"，活动要表达"此刻正在发生"。 */
export const ActivityIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M3 12h4l2.5-7 3 14 2.5-7h6" />
  </Base>
);

export const CalendarIcon = (p: IconProps) => (
  <Base {...p}>
    <rect x="3.5" y="5" width="17" height="15" rx="2" />
    <path d="M3.5 10h17M8 3.5V6M16 3.5V6" />
  </Base>
);

export const CompassIcon = (p: IconProps) => (
  <Base {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="m15.5 8.5-2 5-5 2 2-5 5-2Z" />
  </Base>
);

/**
 * 设置齿轮。
 *
 * 齿是**真的齿**（外圈六颗齿 + 轴心），不是「小圆 + 放射短线」——后者在
 * 18px 下和亮度/太阳图标分不开，在播放器里紧挨着字幕键时尤其容易读成亮度。
 * 六齿而不是八齿：18px 上八颗齿会糊成一圈毛边，六颗才留得住齿廓。
 */
export const GearIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M9.95 5.31L9.13 2.63L14.87 2.63L14.05 5.31A7 7 0 0 1 16.77 6.88L18.68 4.83L21.55 9.8L18.82 10.43A7 7 0 0 1 18.82 13.57L21.55 14.2L18.68 19.17L16.77 17.12A7 7 0 0 1 14.05 18.69L14.87 21.37L9.13 21.37L9.95 18.69A7 7 0 0 1 7.23 17.12L5.32 19.17L2.45 14.2L5.18 13.57A7 7 0 0 1 5.18 10.43L2.45 9.8L5.32 4.83L7.23 6.88A7 7 0 0 1 9.95 5.31Z" />
    <circle cx="12" cy="12" r="3.5" />
  </Base>
);

export const UserIcon = (p: IconProps) => (
  <Base {...p}>
    <circle cx="12" cy="8" r="3.6" />
    <path d="M5 20c1.2-3.4 4-5 7-5s5.8 1.6 7 5" />
  </Base>
);

export const LogoutIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M15 4h3a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-3M10 12h9M16 8l3 4-3 4" />
  </Base>
);

export const ChevronRightIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="m9 6 6 6-6 6" />
  </Base>
);

export const ChevronLeftIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="m15 6-6 6 6 6" />
  </Base>
);

export const ChevronDownIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="m6 9 6 6 6-6" />
  </Base>
);

/** 拖拽手柄（两列圆点）：实心小点比描边更接近系统级「可拖动」示意 */
export const GripIcon = (p: IconProps) => (
  <Base fill="currentColor" stroke="none" {...p}>
    <circle cx="9" cy="6" r="1.4" />
    <circle cx="15" cy="6" r="1.4" />
    <circle cx="9" cy="12" r="1.4" />
    <circle cx="15" cy="12" r="1.4" />
    <circle cx="9" cy="18" r="1.4" />
    <circle cx="15" cy="18" r="1.4" />
  </Base>
);

/** 侧栏开合（面板 + 左侧分栏线）：工作台侧栏「收起 / 展开」的开关图标 */
export const PanelLeftIcon = (p: IconProps) => (
  <Base {...p}>
    <rect x="3" y="4.5" width="18" height="15" rx="2" />
    <path d="M9.5 4.5v15" />
  </Base>
);

export const SearchIcon = (p: IconProps) => (
  <Base {...p}>
    <circle cx="11" cy="11" r="6.5" />
    <path d="m16 16 4 4" />
  </Base>
);

/** 汉堡菜单：移动端顶栏唤起抽屉式侧栏 */
export const MenuIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M4 7h16M4 12h16M4 17h16" />
  </Base>
);

/** 图片（相框 + 山形）：用于图片数量徽标等 */
export const PhotoIcon = (p: IconProps) => (
  <Base {...p}>
    <rect x="3.5" y="5" width="17" height="14" rx="2" />
    <circle cx="9" cy="10" r="1.6" />
    <path d="m6 16.5 4-4 3 3 2.5-2.5 2.5 2.5" />
  </Base>
);

export const ListIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M9 6h11M9 12h11M9 18h11" />
    <path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01" />
  </Base>
);

/** 层叠分组（搜索结果的「分组」视图切换用） */
export const LayersIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="m12 3.5 8 4.2-8 4.2-8-4.2z" />
    <path d="m4 12.4 8 4.2 8-4.2" />
    <path d="m4 16.3 8 4.2 8-4.2" />
  </Base>
);

/** 实心播放三角（用于海报 hover 与 Hero 主按钮，实心比描边更有「按下即播」的分量感） */
export const PlayIcon = (p: IconProps) => (
  <Base fill="currentColor" stroke="none" {...p}>
    <path d="M8 5.5v13a.6.6 0 0 0 .92.5l10.2-6.5a.6.6 0 0 0 0-1L8.92 5a.6.6 0 0 0-.92.5Z" />
  </Base>
);

/** 实心五角星（评分徽章） */
export const StarIcon = (p: IconProps) => (
  <Base fill="currentColor" stroke="none" {...p}>
    <path d="M12 3.6l2.47 5.02 5.53.8-4 3.9.94 5.5L12 16.22l-4.94 2.6.94-5.5-4-3.9 5.53-.8L12 3.6Z" />
  </Base>
);

export const InfoIcon = (p: IconProps) => (
  <Base {...p}>
    <circle cx="12" cy="12" r="8.5" />
    <path d="M12 11v5M12 7.8h.01" />
  </Base>
);

export const ArrowLeftIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M19 12H5M11 6l-6 6 6 6" />
  </Base>
);

export const SendIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M5 12h13M12 6l6 6-6 6" />
  </Base>
);

export const BellIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M6 9a6 6 0 0 1 12 0c0 5 2 6 2 6H4s2-1 2-6M10 20a2 2 0 0 0 4 0" />
  </Base>
);

export const ShieldIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M12 3 5 6v5c0 4.2 2.9 7.6 7 9 4.1-1.4 7-4.8 7-9V6l-7-3Z" />
  </Base>
);

export const PaletteIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M12 3a9 9 0 0 0 0 18c1.7 0 2-1.4 1.2-2.3-.8-.9-.3-2.2 1-2.2H17a4 4 0 0 0 4-4c0-4.4-4-7.5-9-7.5Z" />
    <circle cx="8" cy="12" r="1" />
    <circle cx="12" cy="8" r="1" />
    <circle cx="16" cy="12" r="1" />
  </Base>
);

export const MoreIcon = (p: IconProps) => (
  <Base {...p}>
    <circle cx="5" cy="12" r="1" />
    <circle cx="12" cy="12" r="1" />
    <circle cx="19" cy="12" r="1" />
  </Base>
);

export const PencilIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M17 3a2.4 2.4 0 0 1 3.4 3.4L7.5 19.3 3 21l1.7-4.5Z" />
  </Base>
);

export const TrashIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M4 7h16" />
    <path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
    <path d="M6 7l1 13a1 1 0 0 0 1 .9h8a1 1 0 0 0 1-.9l1-13" />
    <path d="M10 11v6M14 11v6" />
  </Base>
);

export const TerminalIcon = (p: IconProps) => (
  <Base {...p}>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="m7 9 3 3-3 3" />
    <path d="M12.5 15H17" />
  </Base>
);

export const DeviceIcon = (p: IconProps) => (
  <Base {...p}>
    <rect x="3" y="4" width="18" height="12" rx="2" />
    <path d="M2 20h20" />
  </Base>
);

export const GlobeIcon = (p: IconProps) => (
  <Base {...p}>
    <circle cx="12" cy="12" r="9" />
    <path d="M3 12h18" />
    <path d="M12 3c2.6 2.5 4 5.6 4 9s-1.4 6.5-4 9c-2.6-2.5-4-5.6-4-9s1.4-6.5 4-9Z" />
  </Base>
);

export const ServerIcon = (p: IconProps) => (
  <Base {...p}>
    <rect x="3" y="4" width="18" height="7" rx="2" />
    <rect x="3" y="13" width="18" height="7" rx="2" />
    <path d="M7 7.5h.01M7 16.5h.01" />
  </Base>
);

export const FolderIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M3 7a2 2 0 0 1 2-2h4l2.2 2.5H19a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z" />
  </Base>
);

export const DownloadIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M12 4v11" />
    <path d="m7 11 5 4 5-4" />
    <path d="M4 19h16" />
  </Base>
);

/** 升级：DownloadIcon 的垂直镜像（底座不动、箭头朝上），与「下载」成对 */
export const UpgradeIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M12 16V5" />
    <path d="m7 9 5-4 5 4" />
    <path d="M4 19h16" />
  </Base>
);

export const ExpandIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M15 4h5v5" />
    <path d="M20 4l-6 6" />
    <path d="M9 20H4v-5" />
    <path d="m4 20 6-6" />
  </Base>
);

export const ShrinkIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M20 9h-5V4" />
    <path d="m15 9 6-6" />
    <path d="M4 15h5v5" />
    <path d="M9 15l-6 6" />
  </Base>
);

export const RefreshIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M21 12a9 9 0 1 1-2.64-6.36" />
    <path d="M21 3v6h-6" />
  </Base>
);

export const ChatIcon = (p: IconProps) => (
  <Base {...p}>
    <path d="M21 11.5a7.5 7.5 0 0 1-7.5 7.5c-1.06 0-2.07-.2-3-.57L5 20l1.57-4.5A7.5 7.5 0 1 1 21 11.5Z" />
    <path d="M9.5 11.5h.01M13.5 11.5h.01" />
  </Base>
);
