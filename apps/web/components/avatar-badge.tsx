"use client";

import { initialsOf } from "@/lib/session";

/**
 * 用户头像徽标：上传过头像显示图片（圆形裁切），否则回退到昵称首字的银色徽标。
 * 用户菜单、设置页个人信息等所有展示头像处共用本组件，保证换头像后观感一致。
 * 尺寸与字号由调用方通过 className 指定（如 "size-9 text-ui"）。
 *
 * 圆角：默认全圆。要改成别的（Netflix 主题的头像是 4px 圆角方形）必须走
 * style 而不是 className——本组件内部的 rounded-full 与调用方传进来的
 * rounded-[4px] 是同特异度的工具类，拼在一个 class 串里谁赢取决于生成
 * CSS 的先后顺序，实测是 rounded-full 赢，调用方以为改了其实没改。
 */
export function AvatarBadge({
  nickname,
  avatarUrl,
  className = "",
  style,
}: {
  nickname: string;
  avatarUrl: string | null;
  className?: string;
  /** 内联覆盖（目前只用于 borderRadius，见上方说明） */
  style?: React.CSSProperties;
}) {
  return (
    <span
      className={`brand-badge flex shrink-0 items-center justify-center overflow-hidden rounded-full font-bold ${className}`}
      style={style}
    >
      {avatarUrl ? (
        <img
          src={avatarUrl}
          alt={`${nickname} 的头像`}
          decoding="async"
          className="h-full w-full object-cover"
        />
      ) : (
        initialsOf(nickname)
      )}
    </span>
  );
}
