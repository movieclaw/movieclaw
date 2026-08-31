"use client";

import { useRef, useState } from "react";

import { GlassPanel } from "@/components/glass-panel";
import { PlusIcon, SendIcon } from "@/components/icons";
import {
  type ComposerImage,
  MAX_IMAGES_PER_MESSAGE,
  isAcceptedImage,
  prepareImageAttachment,
} from "@/lib/agent-attachments";
import { THINKING_LEVEL_LABELS } from "@/lib/llm-thinking";
import { useBackdrop } from "@/lib/backdrop";
import { LiquidGlassIconButton } from "@/vendor/liquid-glass";

export interface ComposerProps {
  autoFocus?: boolean;
  /** 受控值（与 onChange 配套）；不传则组件内部管理输入状态 */
  value?: string;
  onChange?: (value: string) => void;
  /** 提交回调（回车或点发送）；不传时输入框为纯展示，发送不可用。
   * images 是随消息发送的已上传图片（未开启图片上传时恒为空数组） */
  onSubmit?: (text: string, images: ComposerImage[]) => void;
  /** 开启图片上传：加号键选图，支持粘贴截图与拖拽；图随消息一并提交 */
  imageUpload?: boolean;
  /** 当前模型的思考档位菜单；空/缺省 = 隐藏档位选择器（模型强度不可控） */
  thinkingLevels?: string[];
  /** 当前选中的思考档位；null = 默认（模型自身行为） */
  thinkingValue?: string | null;
  /** 档位切换回调；不传则不渲染选择器 */
  onThinkingChange?: (level: string | null) => void;
  /** 生成中：提交被阻断；配合 onStop 时发送键变为停止键（仿 ChatGPT） */
  busy?: boolean;
  /** 停止生成回调；仅在 busy 时生效 */
  onStop?: () => void;
  /** 锁定：输入与提交全部禁用（如尚未配置 AI 模型时），配合 placeholder 说明原因 */
  disabled?: boolean;
  /** 覆盖默认占位文案（锁定时用于展示提醒） */
  placeholder?: string;
  /** 纯色模式：给阅读面板（对话页）用——不渲染 WebGL 玻璃、不折射背景大图，
   * 避免在不透明底色上把海报纹理透回来；首页氛围页保持默认玻璃形态 */
  flat?: boolean;
}

/* —— 输入框：Codex 风格的无边框输入区（固定 2 行高，超出在框内滚动） —— */
export function Composer({
  autoFocus = false,
  value,
  onChange,
  onSubmit,
  imageUpload = false,
  thinkingLevels,
  thinkingValue = null,
  onThinkingChange,
  busy = false,
  onStop,
  disabled = false,
  placeholder,
  flat = false,
}: ComposerProps) {
  const { backdrop } = useBackdrop();
  // 未受控时的内部状态（纯展示场景仍可直接 <Composer />）
  const [inner, setInner] = useState("");
  const text = value ?? inner;
  const setText = onChange ?? setInner;
  // —— 图片附件（imageUpload 开启时生效）——
  // 图片状态关在组件内部：上传即时发生（拿到 attachment_id），提交时只把
  // 结果交给 onSubmit；父组件不需要感知上传中间态。
  const [images, setImages] = useState<ComposerImage[]>([]);
  const [uploading, setUploading] = useState(0);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const hasImages = images.length > 0;
  const canSubmit =
    !disabled &&
    !busy &&
    uploading === 0 &&
    (text.trim().length > 0 || hasImages) &&
    onSubmit != null;
  // 生成中且可停止：发送键位变为停止键
  const showStop = busy && onStop != null;

  function addFiles(files: Iterable<File>) {
    if (!imageUpload || disabled) return;
    setUploadError(null);
    const accepted = [...files].filter(isAcceptedImage);
    if (accepted.length === 0) {
      setUploadError("不支持的图片格式，请选择 JPG / PNG / WebP / GIF 图片");
      return;
    }
    // 按当前渲染值算余量即可：超发由服务端「单消息 ≤4 张」硬限兜底
    const room = Math.max(0, MAX_IMAGES_PER_MESSAGE - images.length - uploading);
    const taking = accepted.slice(0, room);
    if (accepted.length > taking.length) {
      setUploadError(`一条消息最多发送 ${MAX_IMAGES_PER_MESSAGE} 张图片`);
    }
    for (const file of taking) {
      setUploading((n) => n + 1);
      void prepareImageAttachment(file)
        .then((image) => setImages((now) => [...now, image]))
        .catch((error) => setUploadError((error as Error).message))
        .finally(() => setUploading((n) => n - 1));
    }
  }

  function submit() {
    if (!canSubmit) return;
    onSubmit?.(text.trim(), images);
    setImages([]);
    setUploadError(null);
  }

  const body = (
    <div
      // 拖图进输入区：只在开启图片上传时监听（阻止浏览器默认打开图片）
      onDragOver={imageUpload ? (e) => e.preventDefault() : undefined}
      onDrop={
        imageUpload
          ? (e) => {
              e.preventDefault();
              addFiles(e.dataTransfer.files);
            }
          : undefined
      }
    >
      {(hasImages || uploading > 0) && (
        <div className="flex flex-wrap items-center gap-2 px-4 pt-3">
          {images.map((image) => (
            <div key={image.attachmentId} className="group/chip relative">
              <img
                src={image.previewUrl}
                alt={image.name}
                className="size-14 rounded-lg border border-white/10 object-cover"
              />
              <button
                type="button"
                aria-label={`移除图片 ${image.name}`}
                onClick={() =>
                  setImages((now) => now.filter((it) => it.attachmentId !== image.attachmentId))
                }
                className="absolute -right-1.5 -top-1.5 flex size-5 items-center justify-center rounded-full bg-[#232325] text-[11px] leading-none text-[var(--text-muted)] shadow ring-1 ring-white/15 transition-colors hover:text-[var(--text)]"
              >
                ✕
              </button>
            </div>
          ))}
          {uploading > 0 && (
            <div className="flex size-14 items-center justify-center rounded-lg border border-dashed border-white/15 text-[var(--text-faint)]">
              <span className="size-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
            </div>
          )}
        </div>
      )}
      {uploadError && (
        <p className="px-4 pt-2 text-caption text-[#ff6b6b]">{uploadError}</p>
      )}
      <textarea
        rows={2}
        autoFocus={autoFocus}
        disabled={disabled}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onPaste={
          imageUpload
            ? (e) => {
                // 粘贴截图：剪贴板里带图片文件时收进附件（文字照常走默认粘贴）
                const files = [...e.clipboardData.files].filter(isAcceptedImage);
                if (files.length > 0) {
                  e.preventDefault();
                  addFiles(files);
                }
              }
            : undefined
        }
        onKeyDown={(e) => {
          // 回车提交、Shift+回车换行（输入法组合期间的回车不触发）
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            submit();
          }
        }}
        placeholder={placeholder ?? (busy ? "生成中，可先输入下一条…" : "随心输入，描述一个新任务…")}
        // 锁定态的占位符是提示语（说明为何不可用）而非装饰，按 muted 档渲染保证可读
        className={`scroll-thin block w-full resize-none bg-transparent px-4 pb-1 pt-3.5 text-body leading-6 text-[var(--text)] focus:outline-none ${
          disabled ? "placeholder:text-[var(--text-muted)]" : "placeholder:text-[var(--text-faint)]"
        }`}
      />
      <div className="flex items-center justify-between px-2.5 pb-2.5 pt-1">
        {imageUpload && (
          <input
            ref={fileInputRef}
            type="file"
            accept="image/jpeg,image/png,image/webp,image/gif"
            multiple
            hidden
            onChange={(e) => {
              if (e.target.files) addFiles(e.target.files);
              e.target.value = ""; // 允许重复选同一文件
            }}
          />
        )}
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            aria-label={imageUpload ? "添加图片" : "添加附件"}
            disabled={disabled}
            onClick={imageUpload ? () => fileInputRef.current?.click() : undefined}
            // 移动端 44px：iOS HIG 最小可点目标（桌面保持 32px 紧凑图标键）
            className="flex size-8 items-center justify-center rounded-xl text-[var(--text-muted)] transition-colors hover:bg-[var(--glass-fill-hover)] hover:text-[var(--text)] max-md:size-11"
          >
            <PlusIcon className="size-[18px] max-md:size-[22px]" />
          </button>
          {onThinkingChange && (thinkingLevels?.length ?? 0) > 0 && (
            // 思考档位 pill：菜单按当前模型声明裁剪（服务端推导），空菜单的
            // 模型整个控件不出现——选不到的档位不该存在（maka 同款诚实原则）
            <select
              aria-label="思维链强度"
              disabled={disabled}
              value={thinkingValue ?? ""}
              onChange={(e) => onThinkingChange(e.target.value || null)}
              className="h-8 max-w-[7.5rem] cursor-pointer appearance-none rounded-xl bg-transparent px-2 text-caption text-[var(--text-muted)] outline-none transition-colors hover:bg-[var(--glass-fill-hover)] hover:text-[var(--text)] max-md:h-11"
            >
              <option value="">思考：默认</option>
              {thinkingLevels?.map((level) => (
                <option key={level} value={level}>
                  思考：{THINKING_LEVEL_LABELS[level] ?? level}
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="hidden text-caption text-[var(--text-faint)] sm:block">
            {showStop ? (
              "生成中"
            ) : (
              <>
                <kbd className="tnum rounded bg-white/[0.06] px-1 py-0.5 font-sans">⏎</kbd> 发送
              </>
            )}
          </span>
          {flat ? (
            // 纯色模式的发送 / 停止键：普通实色按钮
            <button
              type="button"
              disabled={!showStop && !canSubmit}
              onClick={() => (showStop ? onStop?.() : submit())}
              aria-label={showStop ? "停止生成" : "发送"}
              // 移动端 44px：iOS HIG 最小可点目标
              className="flex size-9 items-center justify-center rounded-[12px] bg-white/[0.1] text-[var(--text)] transition-colors hover:bg-white/[0.16] disabled:opacity-40 disabled:hover:bg-white/[0.1] max-md:size-11"
            >
              {showStop ? (
                <span className="block size-[11px] rounded-[3px] bg-current" />
              ) : (
                <SendIcon className="size-[18px] max-md:size-[22px]" />
              )}
            </button>
          ) : (
            /* 发送 / 停止：真实 WebGL 液态玻璃按钮，点击经 onActiveChange 触发。 */
            <LiquidGlassIconButton
              backgroundImage={backdrop}
              variant="dark"
              shape="squircle"
              width={36}
              height={36}
              active={false}
              disabled={!showStop && !canSubmit}
              onActiveChange={() => (showStop ? onStop?.() : submit())}
              aria-label={showStop ? "停止生成" : "发送"}
              // WebGL 画布几何跟 width/height 入参走死 36px，视觉不便放大，
              // 用 .touch-target 在移动端把命中区扩到 44px（iOS HIG）
              className="lg-send touch-target !size-9"
            >
              {showStop ? (
                // 停止：ChatGPT 同款实心方块
                <span className="block size-[11px] rounded-[3px] bg-current" />
              ) : (
                <SendIcon className="!size-[18px]" />
              )}
            </LiquidGlassIconButton>
          )}
        </div>
      </div>
    </div>
  );

  if (flat) {
    // 阅读面板上的纯色内嵌卡片：与全站输入框同圆角，发丝描边做边界
    return (
      <div className="rounded-[22px] bg-[var(--surface-inset)] shadow-[inset_0_0_0_1px_rgba(255,255,255,0.07)]">
        {body}
      </div>
    );
  }
  return (
    // 输入框本身是一块真实液态玻璃卡片：折射下方背景大图，浮于内容区之上。
    <GlassPanel
      backgroundImage={backdrop}
      variant="dark"
      radius={22}
      className="composer-shell"
      settings={{ darkTint: 0.42, blur: 0.22, brightness: -0.05 }}
    >
      {body}
    </GlassPanel>
  );
}
