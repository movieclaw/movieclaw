"use client";

/**
 * 全站统一的操作反馈层：Toast 回执 + 确认/输入弹窗。
 *
 * 取代散落各处的 window.alert / window.confirm / window.prompt——原生弹窗
 * 与整站玻璃 UI 语言割裂，移动端体验差，且无法定制危险操作的视觉强度。
 *
 * 设计取舍：
 * - Toast 不用 vendor 的 LiquidGlassToast——那是每实例一个 WebGL 上下文的
 *   重组件（移动 Safari 的 WebGL 上下文上限是个位数，侧栏/按钮已在占用），
 *   短生命周期、可能同时叠多条的 toast 用 CSS 玻璃样式即可，视觉与 Modal
 *   面板同源。
 * - confirm / prompt 是 Promise 风格：`if (!(await confirm({...}))) return;`
 *   与原生用法一行对一行，调用方迁移成本最低。弹窗基于 Modal（raised），
 *   叠在业务弹窗之上也不会被遮挡。
 *
 * 用法：
 *   const toast = useToast();          toast.success("已保存");
 *   const confirm = useConfirm();      if (await confirm({ title, tone: "danger" })) ...
 *   const prompt = usePrompt();        const name = await prompt({ title, initialValue });
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";

import { Modal } from "@/components/modal";

// ---------------------------------------------------------------------------
// Toast
// ---------------------------------------------------------------------------

type ToastTone = "success" | "error" | "info";

/** 可选的行动按钮：点击后 toast 立即消失（如「已提交 → 更改」） */
interface ToastAction {
  label: string;
  onClick: () => void;
}

interface ToastItem {
  id: number;
  tone: ToastTone;
  message: string;
  action?: ToastAction;
}

interface ToastOptions {
  action?: ToastAction;
}

interface ToastApi {
  success: (message: string, options?: ToastOptions) => void;
  error: (message: string, options?: ToastOptions) => void;
  info: (message: string, options?: ToastOptions) => void;
}

// 错误多留两秒：用户需要读完并可能要转述/截图；成功回执扫一眼即可
const TOAST_DURATION_MS: Record<ToastTone, number> = {
  success: 4000,
  info: 4000,
  error: 6000,
};

const TONE_DOT: Record<ToastTone, string> = {
  success: "bg-[var(--ok)]",
  error: "bg-[var(--danger)]",
  info: "bg-white/60",
};

// ---------------------------------------------------------------------------
// 确认 / 输入弹窗
// ---------------------------------------------------------------------------

export interface ConfirmOptions {
  title: string;
  /** 补充说明（后果、影响范围）；支持多行 */
  description?: string;
  /** 将要发生的动作清单（每行一条，带图标点缀）：重操作确认用它代替
   *  大段文字——一眼扫清"点下去会发生什么"。与 description 可并存
   *  （description 作导语）。 */
  bullets?: string[];
  confirmLabel?: string;
  cancelLabel?: string;
  /** danger 时确认键为红色（删除、撤销令牌等破坏性操作） */
  tone?: "default" | "danger";
}

export interface PromptOptions {
  title: string;
  description?: string;
  initialValue?: string;
  placeholder?: string;
  confirmLabel?: string;
  maxLength?: number;
  /** number 时渲染数字输入（数字键盘、隐藏原生步进钮），仍以字符串返回由调用方解析 */
  kind?: "text" | "number";
  /** 数字输入的下限（透传给 input min） */
  min?: number;
  /** 单位后缀（如 "GiB"）：常驻在输入框内部右侧，与输入值视觉上同框不粘连 */
  unit?: string;
}

type DialogRequest =
  | { kind: "confirm"; options: ConfirmOptions; resolve: (ok: boolean) => void }
  | { kind: "prompt"; options: PromptOptions; resolve: (value: string | null) => void };

interface FeedbackApi {
  toast: ToastApi;
  confirm: (options: ConfirmOptions) => Promise<boolean>;
  prompt: (options: PromptOptions) => Promise<string | null>;
}

const FeedbackContext = createContext<FeedbackApi | null>(null);

function useFeedback(): FeedbackApi {
  const ctx = useContext(FeedbackContext);
  if (!ctx) throw new Error("useToast/useConfirm/usePrompt 必须在 FeedbackProvider 内使用");
  return ctx;
}

export function useToast(): ToastApi {
  return useFeedback().toast;
}

export function useConfirm(): (options: ConfirmOptions) => Promise<boolean> {
  return useFeedback().confirm;
}

export function usePrompt(): (options: PromptOptions) => Promise<string | null> {
  return useFeedback().prompt;
}

export function FeedbackProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const [dialog, setDialog] = useState<DialogRequest | null>(null);
  const idRef = useRef(0);
  const timersRef = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    const timer = timersRef.current.get(id);
    if (timer !== undefined) {
      window.clearTimeout(timer);
      timersRef.current.delete(id);
    }
    setToasts((rows) => rows.filter((t) => t.id !== id));
  }, []);

  const push = useCallback(
    (tone: ToastTone, message: string, options?: ToastOptions) => {
      const id = ++idRef.current;
      setToasts((rows) => [...rows.slice(-3), { id, tone, message, action: options?.action }]);
      timersRef.current.set(
        id,
        // 带行动按钮的多留两秒：用户需要时间注意到并点击
        window.setTimeout(() => dismiss(id), TOAST_DURATION_MS[tone] + (options?.action ? 2000 : 0)),
      );
    },
    [dismiss],
  );

  // 卸载时清光计时器（严格模式双挂载下也不会泄漏）
  useEffect(() => {
    const timers = timersRef.current;
    return () => {
      for (const timer of timers.values()) window.clearTimeout(timer);
      timers.clear();
    };
  }, []);

  // 当前在场弹窗的镜像 ref：打开新弹窗时需要同步拿到上一个请求去结算，
  // 走 state 闭包可能读到旧值，ref 始终是最新的
  const dialogRef = useRef<DialogRequest | null>(null);

  /** 打开弹窗。UI 上同一时刻只渲染一个弹窗，若上一个还没被用户处理就来了
   * 新请求（如快捷键与按钮几乎同时触发），先按「取消」结算上一个 Promise，
   * 否则前一个调用方的 await 会永远悬挂。 */
  const openDialog = useCallback((next: DialogRequest) => {
    const previous = dialogRef.current;
    if (previous) {
      if (previous.kind === "confirm") previous.resolve(false);
      else previous.resolve(null);
    }
    dialogRef.current = next;
    setDialog(next);
  }, []);

  const api = useMemo<FeedbackApi>(
    () => ({
      toast: {
        success: (message, options) => push("success", message, options),
        error: (message, options) => push("error", message, options),
        info: (message, options) => push("info", message, options),
      },
      confirm: (options) =>
        new Promise<boolean>((resolve) => openDialog({ kind: "confirm", options, resolve })),
      prompt: (options) =>
        new Promise<string | null>((resolve) => openDialog({ kind: "prompt", options, resolve })),
    }),
    [push, openDialog],
  );

  // 关闭弹窗并回传结果
  const settle = (result: boolean | string | null) => {
    if (!dialog) return;
    if (dialog.kind === "confirm") dialog.resolve(Boolean(result));
    else dialog.resolve(typeof result === "string" ? result : null);
    dialogRef.current = null;
    setDialog(null);
  };

  return (
    <FeedbackContext.Provider value={api}>
      {children}
      {typeof document !== "undefined" &&
        createPortal(
          // z-[95]：压过一切弹层（普通 Modal z-50/60、搜索面板 z-80、
          // 确认弹窗 z-90）——任何层里触发的操作回执都必须可见。
          // 移动端贴底居中并吃安全区；桌面右下角（与系统通知习惯一致）
          <div
            aria-live="polite"
            className="pointer-events-none fixed z-[95] flex flex-col items-center gap-2 max-md:inset-x-4 max-md:bottom-[calc(var(--safe-bottom)+16px)] md:bottom-6 md:right-6 md:items-end"
          >
            {toasts.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => dismiss(t.id)}
                className="toast-item pointer-events-auto flex max-w-[min(92vw,26rem)] items-start gap-2.5 rounded-xl border border-white/10 bg-[rgba(16,18,26,0.92)] px-4 py-3 text-left shadow-[0_18px_50px_rgba(0,0,0,0.55)] backdrop-blur-2xl"
              >
                <span
                  aria-hidden
                  className={`mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[t.tone]}`}
                />
                <span className="min-w-0 text-sub leading-6 text-white/90">{t.message}</span>
                {t.action && (
                  <span
                    role="button"
                    tabIndex={0}
                    onClick={(e) => {
                      e.stopPropagation();
                      dismiss(t.id);
                      t.action?.onClick();
                    }}
                    onKeyDown={(e) => {
                      if (e.key !== "Enter" && e.key !== " ") return;
                      e.stopPropagation();
                      dismiss(t.id);
                      t.action?.onClick();
                    }}
                    className="mt-0.5 shrink-0 rounded-md bg-white/[0.1] px-2 py-0.5 text-caption font-medium text-white transition hover:bg-white/[0.18]"
                  >
                    {t.action.label}
                  </span>
                )}
              </button>
            ))}
          </div>,
          document.body,
        )}
      {dialog?.kind === "confirm" && (
        <ConfirmDialog options={dialog.options} onSettle={(ok) => settle(ok)} />
      )}
      {dialog?.kind === "prompt" && (
        <PromptDialog options={dialog.options} onSettle={(value) => settle(value)} />
      )}
    </FeedbackContext.Provider>
  );
}

/**
 * capture 阶段拦截 Esc：确认/输入弹窗可能叠在搜索面板、业务弹窗之上，
 * 这些下层弹窗各自监听着 Esc（冒泡阶段）——不拦的话一次 Esc 会把整摞全关掉。
 * 见 components/modal.tsx 文件头注释约定的嵌套弹窗协议。
 */
function useEscapeCapture(onEscape: () => void) {
  const handlerRef = useRef(onEscape);
  handlerRef.current = onEscape;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      handlerRef.current();
    };
    window.addEventListener("keydown", onKey, { capture: true });
    return () => window.removeEventListener("keydown", onKey, { capture: true });
  }, []);
}

/** 底栏按钮的公共样式（确认/输入弹窗共用） */
const CANCEL_BTN_CLS =
  "rounded-lg border border-white/10 bg-white/[0.06] px-4 py-2 text-ui text-white/80 transition hover:bg-white/[0.1]";

function confirmButtonCls(tone: ConfirmOptions["tone"]): string {
  return tone === "danger"
    ? "rounded-lg bg-red-500/85 px-4 py-2 text-ui font-medium text-white transition hover:bg-red-500"
    : "rounded-lg bg-white/90 px-4 py-2 text-ui font-medium text-black transition hover:bg-white";
}

function ConfirmDialog({
  options,
  onSettle,
}: {
  options: ConfirmOptions;
  onSettle: (ok: boolean) => void;
}) {
  // 初始焦点给取消键：危险操作按 Enter 不应直接生效，多一步指向性点击
  const cancelRef = useRef<HTMLButtonElement>(null);
  useEffect(() => cancelRef.current?.focus(), []);
  useEscapeCapture(() => onSettle(false));

  return (
    <Modal open onClose={() => onSettle(false)} label={options.title} topmost>
      <div className="p-6 max-md:p-5">
        <h2 className="text-title-sm font-bold text-white">{options.title}</h2>
        {options.description && (
          <p className="mt-2 whitespace-pre-line text-sub leading-6 text-[var(--text-muted)]">
            {options.description}
          </p>
        )}
        {options.bullets && options.bullets.length > 0 && (
          <ul className="mt-3 space-y-2">
            {options.bullets.map((line) => (
              <li key={line} className="flex items-start gap-2.5">
                {/* 小对勾图标：内联 SVG 不引外部依赖，accent 色点缀。
                    危险确认的清单是"将被销毁的东西"，打勾会读成"已完成"，
                    换成一枚危险色圆点。 */}
                {options.tone === "danger" ? (
                  <span
                    aria-hidden
                    className="mt-[9px] size-1.5 shrink-0 rounded-full bg-[var(--danger)]"
                  />
                ) : (
                  <svg
                    aria-hidden
                    viewBox="0 0 16 16"
                    className="mt-[5px] size-3.5 shrink-0 text-[var(--accent)]"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  >
                    <path d="M2.5 8.5l3.5 3.5 7.5-8" />
                  </svg>
                )}
                <span className="min-w-0 text-sub leading-6 text-[var(--text-muted)]">{line}</span>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-5 flex justify-end gap-2.5">
          <button
            ref={cancelRef}
            type="button"
            onClick={() => onSettle(false)}
            className={CANCEL_BTN_CLS}
          >
            {options.cancelLabel ?? "取消"}
          </button>
          <button
            type="button"
            onClick={() => onSettle(true)}
            className={confirmButtonCls(options.tone)}
          >
            {options.confirmLabel ?? "确定"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function PromptDialog({
  options,
  onSettle,
}: {
  options: PromptOptions;
  onSettle: (value: string | null) => void;
}) {
  const [value, setValue] = useState(options.initialValue ?? "");
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, []);
  useEscapeCapture(() => onSettle(null));

  const submit = () => onSettle(value);

  return (
    <Modal open onClose={() => onSettle(null)} label={options.title} topmost>
      <form
        className="p-6 max-md:p-5"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <h2 className="text-title-sm font-bold text-white">{options.title}</h2>
        {options.description && (
          <p className="mt-2 text-sub leading-6 text-[var(--text-muted)]">{options.description}</p>
        )}
        {/* 单位后缀方案：input 包进 relative 容器，单位常驻输入框内部右侧
            （用 padding-right 给它留位），数字模式隐藏原生步进钮保持简洁 */}
        <div className="relative mt-4">
          <input
            ref={inputRef}
            value={value}
            type={options.kind === "number" ? "number" : "text"}
            inputMode={options.kind === "number" ? "numeric" : undefined}
            min={options.kind === "number" ? options.min : undefined}
            maxLength={options.maxLength}
            placeholder={options.placeholder}
            onChange={(e) => setValue(e.target.value)}
            className={`w-full rounded-lg border border-white/10 bg-white/[0.06] px-3.5 py-2.5 text-ui text-white outline-none transition focus:border-white/25 ${
              options.unit ? "pr-14" : ""
            } ${
              options.kind === "number"
                ? "[appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
                : ""
            }`}
          />
          {options.unit && (
            <span className="pointer-events-none absolute inset-y-0 right-3.5 flex items-center text-sub font-medium text-[var(--text-faint)]">
              {options.unit}
            </span>
          )}
        </div>
        <div className="mt-5 flex justify-end gap-2.5">
          <button type="button" onClick={() => onSettle(null)} className={CANCEL_BTN_CLS}>
            取消
          </button>
          <button type="submit" className={confirmButtonCls("default")}>
            {options.confirmLabel ?? "确定"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
