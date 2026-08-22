"use client";

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  listDownloadTasks,
  type DownloadTask,
  type DownloadTaskSnapshot,
  type DownloadTaskSource,
} from "@/lib/api/downloaders";
import { useSession } from "@/lib/session";
import { useVisiblePolling } from "@/lib/use-visible-polling";

const POLL_INTERVAL_MS = 10_000;

interface DownloadTasksContextValue {
  tasks: DownloadTask[];
  sources: DownloadTaskSource[];
  subscriptionTasks: DownloadTask[];
  loading: boolean;
  error: string | null;
  refreshedAt: number | null;
  refresh: () => void;
}

const DownloadTasksContext = createContext<DownloadTasksContextValue | null>(null);

/**
 * 全站唯一下载任务快照。页面可见时每 10 秒读取一次下载器；多个消费方
 * （侧栏快速视图、完整任务中心）共享结果，不会各自重复请求外部下载器。
 */
export function DownloadTasksProvider({ children }: { children: React.ReactNode }) {
  const { session } = useSession();
  const enabled = session.role !== "member";
  const [snapshot, setSnapshot] = useState<DownloadTaskSnapshot>({ items: [], sources: [] });
  const [loading, setLoading] = useState(enabled);
  const [error, setError] = useState<string | null>(null);
  const [refreshedAt, setRefreshedAt] = useState<number | null>(null);
  const pending = useRef(false);
  const queued = useRef(false);

  const refresh = useCallback(() => {
    if (!enabled) return;
    queued.current = true;
    if (pending.current) return;
    pending.current = true;
    void (async () => {
      try {
        while (queued.current) {
          queued.current = false;
          try {
            const next = await listDownloadTasks();
            setSnapshot(next);
            setError(null);
            setRefreshedAt(Date.now());
          } catch (caught) {
            setError((caught as Error).message || "下载任务加载失败");
          } finally {
            setLoading(false);
          }
        }
      } finally {
        pending.current = false;
      }
    })();
  }, [enabled]);

  useVisiblePolling(refresh, enabled ? POLL_INTERVAL_MS : null, { leading: true });

  const subscriptionTasks = useMemo(
    () => snapshot.items.filter((task) => task.source === "subscription"),
    [snapshot.items],
  );
  const value = useMemo(
    () => ({
      tasks: snapshot.items,
      sources: snapshot.sources,
      subscriptionTasks,
      loading,
      error,
      refreshedAt,
      refresh,
    }),
    [
      snapshot,
      subscriptionTasks,
      loading,
      error,
      refreshedAt,
      refresh,
    ],
  );
  return <DownloadTasksContext.Provider value={value}>{children}</DownloadTasksContext.Provider>;
}

export function useDownloadTasks(): DownloadTasksContextValue {
  const value = useContext(DownloadTasksContext);
  if (!value) throw new Error("useDownloadTasks 必须在 DownloadTasksProvider 内使用");
  return value;
}
