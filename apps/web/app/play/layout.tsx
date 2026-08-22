import { AuthGate } from "@/components/auth-gate";

/**
 * 播放路由组：**刻意不套工作台外壳**。
 *
 * 播放器要独占整块画面，侧栏、顶栏、背景大图在这里全是干扰；全屏时它们还会
 * 与系统控件打架。所以 /play 与 /login、/setup 一样留在 (app) 组之外，只保留
 * 登录闸门。
 */
export default function PlayLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGate>
      <div className="h-dvh w-full overflow-hidden bg-black">{children}</div>
    </AuthGate>
  );
}
