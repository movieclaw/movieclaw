"use client";

import { useCallback, useEffect, useState } from "react";

import { AuthError, AuthField, AuthScreen } from "@/components/auth-screen";
import { SharedItemView } from "@/components/share/shared-item-view";
import { probeShare, unlockShare } from "@/lib/api/shares";
import { HttpError } from "@/lib/http";
import { usePageTitle } from "@/lib/use-page-title";

type Phase =
  | { kind: "loading" }
  | { kind: "locked" }
  | { kind: "unavailable"; message: string }
  | { kind: "ready" };

/** 探针失败 → 访客看得懂的一句话（后端 message 已是中文，直接用）。 */
export function unavailableMessage(error: unknown): string {
  if (error instanceof HttpError) {
    if (error.status === 404) return error.message || "分享不存在或已取消";
    return error.message || "暂时无法打开这条分享，请稍后再试";
  }
  return "无法连接到服务器，请检查网络后重试";
}

/**
 * 影片分享页的状态机（docs/design/media-share.md §1.2）：
 *
 *   探针 → 需要密码且未解锁 → 密码卡片（密码之前不露片名海报）
 *       → 失效（不存在 / 已取消 / 已过期）→ 整页提示
 *       → 可看 → 影片页
 */
export function SharePage({ slug }: { slug: string }) {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });

  const probe = useCallback(() => {
    probeShare(slug)
      .then((info) =>
        setPhase(info.requires_password && !info.unlocked ? { kind: "locked" } : { kind: "ready" }),
      )
      .catch((error: unknown) =>
        setPhase({ kind: "unavailable", message: unavailableMessage(error) }),
      );
  }, [slug]);

  useEffect(() => {
    setPhase({ kind: "loading" });
    probe();
  }, [probe]);

  if (phase.kind === "ready") return <SharedItemView slug={slug} />;
  if (phase.kind === "locked") {
    return <ShareGate slug={slug} onUnlocked={() => setPhase({ kind: "ready" })} />;
  }
  if (phase.kind === "unavailable") return <ShareUnavailable message={phase.message} />;
  return (
    <div className="flex min-h-dvh items-center justify-center gap-2.5 text-ui text-white/60">
      <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
      正在打开分享…
    </div>
  );
}

/** 密码卡片：与登录页同一个外壳，但没有任何站内入口。 */
function ShareGate({ slug, onUnlocked }: { slug: string; onUnlocked: () => void }) {
  usePageTitle("需要密码");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!password.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await unlockShare(slug, password.trim());
      onUnlocked();
    } catch (e) {
      setError(
        e instanceof HttpError
          ? e.message || "密码不对，请重新输入"
          : "无法连接到服务器，请检查网络后重试",
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <AuthScreen title="需要密码" subtitle="这是一条受密码保护的分享，输入密码后即可观看。">
      <form onSubmit={submit} className="space-y-4">
        <AuthField
          label="密码"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="off"
          autoFocus
          required
        />
        <AuthError message={error} />
        <button
          type="submit"
          disabled={busy || !password.trim()}
          className="w-full rounded-xl bg-white px-4 py-2.5 text-ui font-semibold text-black transition hover:bg-white/90 disabled:opacity-50"
        >
          {busy ? "正在验证…" : "打开"}
        </button>
      </form>
    </AuthScreen>
  );
}

function ShareUnavailable({ message }: { message: string }) {
  usePageTitle("分享不可用");
  return (
    <AuthScreen title={message} subtitle="请向分享者确认链接是否仍然有效。">
      <p className="text-caption text-[var(--text-faint)]">
        分享链接有有效期，到期或被取消后就无法再打开。
      </p>
    </AuthScreen>
  );
}
