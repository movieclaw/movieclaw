/**
 * 影片分享页路由组（docs/design/media-share.md §1.2）：**刻意不套工作台外壳、
 * 不挂登录闸门**。访客没有账号，页面上没有侧栏、导航与任何站内入口——
 * 只能停在这一部影片上。也不挂 FeedbackProvider：分享页没有需要提示 / 确认
 * 弹窗的动作，而它的 toast 容器是客户端 portal，在服务端首帧里直接水合会报
 * 不一致（AppShell 里它从不被服务端渲染，所以那边没这个问题）。
 */
export default function ShareLayout({ children }: { children: React.ReactNode }) {
  return <div className="min-h-dvh w-full bg-[#07080c] text-white">{children}</div>;
}
