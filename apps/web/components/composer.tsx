"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { GlassPanel } from "@/components/glass-panel";
import { ChevronRightIcon, PlusIcon, SendIcon } from "@/components/icons";
import {
  type ComposerImage,
  MAX_IMAGES_PER_MESSAGE,
  isAcceptedImage,
  prepareImageAttachment,
} from "@/lib/agent-attachments";
import { ComposerEditor, type ComposerEditorHandle } from "@/components/composer-editor";
import { listSkills, type AgentSkill } from "@/lib/api/agent";
import type { LlmModelOption } from "@/lib/api/llm";
import { THINKING_LEVEL_LABELS, resolveModelOption } from "@/lib/llm-thinking";
import {
  steppedStopIndex,
  stopIndexAtPointer,
  stopPercent,
  thinkingControlShape,
  thinkingListItems,
  thinkingListNote,
  thinkingPillLabel,
  thinkingStops,
} from "@/lib/thinking-level-control";
import { useBackdrop } from "@/lib/backdrop";
import { LiquidGlassIconButton } from "@/vendor/liquid-glass";

/*
 * 布局设计（调研 maka composer 与 Codex 输入框后的定案）：
 *
 *   ┌─ 附件托盘（有附件才出现）：小型 chip（缩略图 + 名字 + ×），不放大图 ─┐
 *   ├─ 输入区：无边框 textarea（Codex 风格，固定 2 行高框内滚动）        ─┤
 *   └─ 工具行（单行，永不换行/reflow）：                                  ─┘
 *        左簇：＋（图片/技能菜单） · 模型（ghost pill） · 思考档位（ghost pill）
 *        右簇：回车提示 · 发送/停止
 *
 * 三条从 maka 学来的原则：
 * 1. 控件都是「安静的」ghost pill——描边与底色到 hover 才出现，弹层一律向上；
 * 2. 能力不可用时整个控件不渲染（思考菜单为空、未开图片上传），而不是禁用态；
 *    生成中则只 disable 不卸载，工具行不因运行状态改变布局；
 * 3. 附件是紧凑 chip 而非大缩略图：输入框是写字的地方，预览细节交给会话气泡。
 */

export interface ComposerProps {
  autoFocus?: boolean;
  /** 受控值（与 onChange 配套）；不传则组件内部管理输入状态 */
  value?: string;
  onChange?: (value: string) => void;
  /** 提交回调（回车或点发送）；不传时输入框为纯展示，发送不可用。
   * images 是随消息发送的已上传图片（未开启图片上传时恒为空数组） */
  onSubmit?: (text: string, images: ComposerImage[]) => void;
  /** 开启图片上传：加号菜单里出现「上传图片」，支持粘贴截图与拖拽；图随消息一并提交 */
  imageUpload?: boolean;
  /** 开启技能选择：加号菜单里列出可显式调用的技能，选中在光标处插入
   * /skill:名字 占位符（服务端发送时展开，docs/design/agent-skills.md §9） */
  skillPicker?: boolean;
  /** 可选模型清单（GET /llm/models 拍平结果）；空/缺省 = 隐藏模型选择器 */
  modelOptions?: LlmModelOption[];
  /** 当前选中的模型引用（选项的 ref）；null = 默认（默认实例的默认模型） */
  modelValue?: string | null;
  /** 模型切换回调；不传则不渲染选择器 */
  onModelChange?: (ref: string | null) => void;
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
  skillPicker = false,
  modelOptions,
  modelValue = null,
  onModelChange,
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
  const editorRef = useRef<ComposerEditorHandle>(null);
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

  /** 在光标处插入技能 token chip；插入后光标停在其后（加号菜单入口）。 */
  function insertSkill(name: string) {
    editorRef.current?.insertSkill(name);
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
      {/* 附件托盘：有内容才占高度，错误提示与 chips 同区（不各自另起一行） */}
      {(hasImages || uploading > 0 || uploadError) && (
        <div className="flex flex-wrap items-center gap-2 px-3.5 pt-3">
          {images.map((image) => (
            <AttachmentChip
              key={image.attachmentId}
              image={image}
              onRemove={() =>
                setImages((now) => now.filter((it) => it.attachmentId !== image.attachmentId))
              }
            />
          ))}
          {uploading > 0 && (
            <span className="flex h-9 items-center gap-2 rounded-lg bg-white/[0.05] px-2.5 text-caption text-[var(--text-faint)]">
              <span className="size-3.5 animate-spin rounded-full border-2 border-current border-t-transparent" />
              上传中…
            </span>
          )}
          {uploadError && (
            <span role="status" className="text-caption text-[#ff6b6b]">
              {uploadError}
            </span>
          )}
        </div>
      )}
      {/* Lexical 编辑器：对外仍是纯文本契约（含 /skill: 占位符），内部把
          占位符渲染成技能 chip；输入「/」触发技能快捷菜单 */}
      <ComposerEditor
        ref={editorRef}
        value={text}
        onChange={setText}
        onSubmit={submit}
        autoFocus={autoFocus}
        disabled={disabled}
        skillPicker={skillPicker && !disabled}
        onPasteFiles={
          imageUpload
            ? (files) => {
                // 粘贴截图：剪贴板里带图片文件时收进附件（文字照常走默认粘贴）
                const accepted = [...files].filter(isAcceptedImage);
                if (accepted.length > 0) addFiles(accepted);
              }
            : undefined
        }
        // 锁定态的占位符是提示语（说明为何不可用）而非装饰，按 muted 档渲染保证可读
        placeholder={
          placeholder ?? (busy ? "生成中，可先输入下一条…" : "随心输入，描述一个新任务…")
        }
      />
      {/* 工具行：左控件簇 + 右发送簇，单行永不换行 */}
      <div className="flex items-center justify-between gap-2 px-2.5 pb-2.5 pt-1">
        <div className="flex min-w-0 items-center gap-1">
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
          {(imageUpload || skillPicker) && (
            <ComposerPlusMenu
              imageUpload={imageUpload}
              skillPicker={skillPicker}
              disabled={disabled}
              onPickImage={() => fileInputRef.current?.click()}
              onPickSkill={insertSkill}
            />
          )}
          {onModelChange && (modelOptions?.length ?? 0) > 0 && (
            <ModelMenu
              options={modelOptions ?? []}
              value={modelValue}
              disabled={disabled}
              onChange={onModelChange}
            />
          )}
          {onThinkingChange && (thinkingLevels?.length ?? 0) > 0 && (
            <ThinkingLevelMenu
              levels={thinkingLevels ?? []}
              value={thinkingValue}
              disabled={disabled}
              onChange={onThinkingChange}
            />
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
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

/* —— 附件 chip：小缩略图 + 截断名 + 移除键（maka Token 的紧凑节奏） —— */

function AttachmentChip({ image, onRemove }: { image: ComposerImage; onRemove: () => void }) {
  return (
    <span className="group/chip flex h-9 max-w-[13rem] items-center gap-2 rounded-lg bg-white/[0.05] py-1 pl-1 pr-1 shadow-[inset_0_0_0_1px_rgba(255,255,255,0.06)]">
      <img
        src={image.previewUrl}
        alt=""
        className="size-7 shrink-0 rounded-md object-cover"
      />
      <span className="min-w-0 truncate text-caption text-[var(--text-muted)]" title={image.name}>
        {image.name}
      </span>
      <button
        type="button"
        aria-label={`移除图片 ${image.name}`}
        onClick={onRemove}
        className="flex size-5 shrink-0 items-center justify-center rounded-md text-[11px] leading-none text-[var(--text-faint)] transition-colors hover:bg-white/[0.08] hover:text-[var(--text)]"
      >
        ✕
      </button>
    </span>
  );
}

/* —— 加号菜单：上传图片 + 技能选择（docs/design/agent-skills.md §9.2） ——
 * 技能列表在每次展开时现拉（与服务端「改技能即生效」语义一致，不做缓存）；
 * 选中技能在光标处插入 /skill:名字 占位符，发送时由服务端展开。
 * 弹层同 ThinkingLevelMenu：Portal 到 body + fixed 定位，躲开玻璃面板的
 * overflow:hidden 裁切。 */

function ComposerPlusMenu({
  imageUpload,
  skillPicker,
  disabled,
  onPickImage,
  onPickSkill,
}: {
  imageUpload: boolean;
  skillPicker: boolean;
  disabled?: boolean;
  onPickImage: () => void;
  onPickSkill: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [menuPos, setMenuPos] = useState<{ left: number; bottom: number } | null>(null);
  // null = 加载中；[] = 无技能；undefined = 未启用技能选择
  const [skills, setSkills] = useState<AgentSkill[] | null | undefined>(
    skillPicker ? null : undefined,
  );
  const [skillsError, setSkillsError] = useState(false);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (rootRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const toggleOpen = () => {
    if (!open && rootRef.current) {
      const rect = rootRef.current.getBoundingClientRect();
      setMenuPos({ left: rect.left, bottom: window.innerHeight - rect.top + 8 });
      if (skillPicker) {
        setSkills(null);
        setSkillsError(false);
        listSkills()
          .then(setSkills)
          .catch(() => {
            setSkills([]);
            setSkillsError(true);
          });
      }
    }
    setOpen((v) => !v);
  };

  const menu = open && menuPos && (
    <div
      ref={menuRef}
      role="menu"
      aria-label="添加内容"
      className="menu-surface max-h-72 min-w-[15rem] max-w-[22rem] overflow-y-auto p-1.5"
      style={{ position: "fixed", left: menuPos.left, bottom: menuPos.bottom, zIndex: 50 }}
    >
      {imageUpload && (
        <button
          type="button"
          role="menuitem"
          onClick={() => {
            onPickImage();
            setOpen(false);
          }}
          className="flex w-full items-center gap-2 rounded-[10px] px-2.5 py-1.5 text-left text-ui text-[var(--text)] transition-colors hover:bg-white/[0.06]"
        >
          上传图片
        </button>
      )}
      {skillPicker && (
        <>
          {imageUpload && <div className="mx-2.5 my-1 h-px bg-white/[0.08]" />}
          <p className="px-2.5 pb-0.5 pt-1 text-caption text-[var(--text-faint)]">使用技能</p>
          {skills === null && (
            <p className="px-2.5 py-1.5 text-caption text-[var(--text-faint)]">加载中…</p>
          )}
          {skills != null && skills.length === 0 && (
            <p className="px-2.5 py-1.5 text-caption text-[var(--text-faint)]">
              {skillsError ? "技能列表加载失败" : "暂无可用技能"}
            </p>
          )}
          {skills?.map((skill) => (
            <button
              key={skill.name}
              type="button"
              role="menuitem"
              title={skill.description}
              onClick={() => {
                onPickSkill(skill.name);
                setOpen(false);
              }}
              className="block w-full rounded-[10px] px-2.5 py-1.5 text-left transition-colors hover:bg-white/[0.06]"
            >
              <span className="block text-ui text-[var(--text)]">⚡ {skill.name}</span>
              <span className="block truncate text-caption text-[var(--text-muted)]">
                {skill.description}
              </span>
            </button>
          ))}
        </>
      )}
    </div>
  );

  return (
    <div ref={rootRef} className="relative shrink-0">
      {menu && createPortal(menu, document.body)}
      <button
        type="button"
        aria-label="添加图片或技能"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={toggleOpen}
        // 移动端 44px：iOS HIG 最小可点目标（桌面保持 32px 紧凑图标键）
        className="flex size-8 shrink-0 items-center justify-center rounded-xl text-[var(--text-muted)] transition-colors hover:bg-[var(--glass-fill-hover)] hover:text-[var(--text)] max-md:size-11"
      >
        <PlusIcon className="size-[18px] max-md:size-[22px]" />
      </button>
    </div>
  );
}

/* —— 锚定弹层：ghost pill 上方的浮层，模型菜单与思维链滑杆共用 ——
 * 弹层 Portal 到 body + fixed 定位（同 user-menu 折叠态）：composer 包在
 * GlassPanel 里，面板 overflow:hidden 会把向上的弹层裁掉。打开瞬间按 pill
 * 当前位置算一次坐标（浮层是瞬态的，不跟随滚动）；点弹层外任意处或 Escape 收起。 */

function useAnchoredPopover() {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; bottom: number } | null>(null);

  // Portal 出去的弹层不在 rootRef 内，需单独判断
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node;
      if (rootRef.current?.contains(target) || popoverRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const toggle = () => {
    if (!open && rootRef.current) {
      const rect = rootRef.current.getBoundingClientRect();
      setPos({ left: rect.left, bottom: window.innerHeight - rect.top + 8 });
    }
    setOpen((v) => !v);
  };

  return { open, toggle, close: () => setOpen(false), rootRef, popoverRef, pos };
}

/** 工具行里的安静 pill：描边与底色到 hover 才出现 */
const PILL_CLASS =
  "flex h-8 max-w-full items-center gap-1 rounded-xl px-2.5 text-caption text-[var(--text-muted)] transition-colors hover:bg-[var(--glass-fill-hover)] hover:text-[var(--text)] max-md:h-11";

/* —— 安静菜单：ghost pill + 向上弹出的单选列表（maka quiet-menu 的思路） ——
 * 不用原生 <select>：弹层要向上、选中项要打勾、pill 文案要与菜单项分离
 * （pill 只显示当前项，菜单里才是完整清单），原生控件三样都做不到。 */

interface QuietMenuOption {
  key: string;
  label: string;
  selected: boolean;
  onSelect: () => void;
  /** 第二行灰字：这一项到底做什么（思考开关这种一个词说不清的用） */
  description?: string;
}

function QuietMenu({
  ariaLabel,
  pillLabel,
  options,
  disabled,
  wide = false,
  note,
}: {
  ariaLabel: string;
  pillLabel: string;
  options: QuietMenuOption[];
  disabled?: boolean;
  /** 模型清单可能很长：弹层放宽并限高滚动 */
  wide?: boolean;
  /** 清单底部的一行备注（如「该模型只能开关思考」） */
  note?: string | null;
}) {
  const { open, toggle, close, rootRef, popoverRef, pos } = useAnchoredPopover();

  // 长清单（模型）打开时把当前项滚进视野，用户一眼看到自己选的是哪个
  useEffect(() => {
    if (!open) return;
    popoverRef.current
      ?.querySelector<HTMLElement>('[aria-selected="true"]')
      ?.scrollIntoView({ block: "nearest" });
  }, [open, popoverRef]);

  const menu = open && pos && (
    <div
      ref={popoverRef}
      role="listbox"
      aria-label={ariaLabel}
      className={`menu-surface p-1.5 ${
        wide ? "max-h-72 min-w-[12rem] max-w-[22rem] overflow-y-auto" : "min-w-[8rem]"
      }`}
      // .menu-surface 自带 position:relative，须整体覆盖为 fixed
      style={{ position: "fixed", left: pos.left, bottom: pos.bottom, zIndex: 50 }}
    >
      {options.map((option) => (
        <button
          key={option.key}
          type="button"
          role="option"
          aria-selected={option.selected}
          title={option.label}
          onClick={() => {
            option.onSelect();
            close();
          }}
          className={`flex w-full items-center justify-between gap-3 rounded-[10px] px-2.5 py-1.5 text-left text-ui transition-colors hover:bg-white/[0.06] ${
            option.selected ? "text-[var(--text)]" : "text-[var(--text-muted)]"
          }`}
        >
          <span className="min-w-0">
            <span className="block truncate">{option.label}</span>
            {option.description && (
              <span className="block text-caption leading-4 text-[var(--text-faint)]">
                {option.description}
              </span>
            )}
          </span>
          {option.selected && <span aria-hidden>✓</span>}
        </button>
      ))}
      {note && (
        <p className="mx-1 mt-1 border-t border-white/[0.08] px-1.5 pb-0.5 pt-2 text-caption text-[var(--text-faint)]">
          {note}
        </p>
      )}
    </div>
  );

  return (
    <div ref={rootRef} className="relative min-w-0 shrink">
      {menu && createPortal(menu, document.body)}
      <button
        type="button"
        aria-label={`${ariaLabel}：${pillLabel}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        disabled={disabled}
        onClick={toggle}
        className={PILL_CLASS}
      >
        <span className="min-w-0 truncate">{pillLabel}</span>
        <ChevronRightIcon
          className={`size-3 shrink-0 transition-transform ${open ? "rotate-[-90deg]" : "rotate-90"}`}
        />
      </button>
    </div>
  );
}

/* —— 模型选择：清单由服务端拍平，pill 只显示当前项的 label。
 * 未显式选择（value=null）时显示全局默认项；同 id 跨实例冲突的项 label 已带
 * 「（实例名）」，不冲突的就是裸模型 id——前端不做去重判断。 */

function ModelMenu({
  options,
  value,
  disabled,
  onChange,
}: {
  options: LlmModelOption[];
  value: string | null;
  disabled?: boolean;
  onChange: (ref: string | null) => void;
}) {
  const current = resolveModelOption(options, value);
  return (
    <QuietMenu
      ariaLabel="模型"
      pillLabel={current?.label ?? "模型"}
      disabled={disabled}
      wide
      options={options.map((option) => ({
        key: option.ref,
        label: option.label,
        selected: option.ref === current?.ref,
        // 选回全局默认项即「默认」（null）：续聊沿用、设置页改默认后自动跟随
        onSelect: () => onChange(option.is_default ? null : option.ref),
      }))}
    />
  );
}

/* —— 思维链强度：pill 只显示当前档位本身；弹层形态由菜单形状决定 ——
 * 强度是有序量（越右想得越深、越慢），两档以上用一根横向离散滑杆：
 *   标题行  「强度  高」            右侧「恢复默认」（选了档位才出现）
 *   轴标签  「更快 ……… 更聪明」
 *   滑杆     ●──●──◉──●──●   刻度 = 该模型声明的档位，按统一词汇表排序
 * 轨道整体可点可拖（指针捕获，滑出轨道也跟手），松手前一直吸附到最近刻度；
 * 键盘左右键在刻度间移动。「默认」= 不发任何参数、用模型自身行为，不是强度
 * 轴上的一点，所以不占刻度：默认态滑杆无滑块，靠「恢复默认」回去。
 * 只有一档的模型（toggle 方言的「关」）没有可拖的距离，画成滑杆点了就
 * 回不到默认——改用与模型选择器同款的两项列表「默认 / 关」。 */

/** 刻度圆心离轨道两端的留白（px）：滑块半径 14px 加一点边距，与定位式共用。 */
const TRACK_INSET_PX = 16;

function ThinkingLevelMenu({
  levels,
  value,
  disabled,
  onChange,
}: {
  levels: string[];
  value: string | null;
  disabled?: boolean;
  onChange: (level: string | null) => void;
}) {
  const stops = thinkingStops(levels);
  const currentLabel = thinkingPillLabel(stops, value);
  if (thinkingControlShape(stops) === "list") {
    // 只能开关的模型：「默认」说不清是开是关，列表写成「开启（模型默认）/ 关闭」
    // 并各带一行说明，底部注明没有强度档位（文案在 thinking-level-control.ts）
    return (
      <QuietMenu
        ariaLabel="思维链"
        pillLabel={currentLabel}
        disabled={disabled}
        wide
        note={thinkingListNote(stops)}
        options={thinkingListItems(stops).map((item) => ({
          key: item.level ?? "default",
          label: item.label,
          description: item.description,
          selected: value === item.level,
          onSelect: () => onChange(item.level),
        }))}
      />
    );
  }
  return (
    <ThinkingLevelSlider
      stops={stops}
      value={value}
      currentLabel={currentLabel}
      disabled={disabled}
      onChange={onChange}
    />
  );
}

function ThinkingLevelSlider({
  stops,
  value,
  currentLabel,
  disabled,
  onChange,
}: {
  stops: string[];
  value: string | null;
  currentLabel: string;
  disabled?: boolean;
  onChange: (level: string | null) => void;
}) {
  const { open, toggle, rootRef, popoverRef, pos } = useAnchoredPopover();
  const trackRef = useRef<HTMLDivElement>(null);
  // 指针按下到抬起之间为拖拽态；用 ref 而不是 state，move 事件里读最新值且不触发重渲染
  const dragging = useRef(false);
  const index = value === null ? -1 : stops.indexOf(value);
  const percent = (i: number) => stopPercent(i, stops.length);
  const stopLeft = (i: number) =>
    `calc(${TRACK_INSET_PX}px + (100% - ${TRACK_INSET_PX * 2}px) * ${percent(i) / 100})`;

  /** 把指针横坐标吸附到最近刻度并选中；值没变时不回调，拖动时不会刷屏更新。 */
  const pickAt = (clientX: number) => {
    const rect = trackRef.current?.getBoundingClientRect();
    if (!rect) return;
    const next =
      stops[stopIndexAtPointer(clientX, rect.left, rect.width, stops.length, TRACK_INSET_PX)];
    if (next !== value) onChange(next);
  };
  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.focus();
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // 无活动指针（合成事件等）时 capture 会抛错；丢失捕获只影响指针滑出
      // 轨道后的跟踪，点选本身照常
    }
    dragging.current = true;
    pickAt(event.clientX);
  };
  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (dragging.current) pickAt(event.clientX);
  };
  const onPointerEnd = (event: React.PointerEvent<HTMLDivElement>) => {
    dragging.current = false;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  };
  const onKeyDown = (event: React.KeyboardEvent) => {
    const next = steppedStopIndex(index, stops.length, event.key);
    if (next === null) return;
    event.preventDefault();
    onChange(stops[next]);
  };

  const panel = open && pos && (
    <div
      ref={popoverRef}
      role="group"
      aria-label="思维链强度"
      className="menu-surface w-[18rem] p-4"
      style={{ position: "fixed", left: pos.left, bottom: pos.bottom, zIndex: 50 }}
    >
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-ui text-[var(--text-muted)]">
          强度
          <span className="ml-2 text-body font-medium text-[var(--text)]">{currentLabel}</span>
        </p>
        {value !== null ? (
          <button
            type="button"
            onClick={() => onChange(null)}
            className="rounded-md px-1.5 py-0.5 text-caption text-[var(--text-faint)] transition-colors hover:bg-white/[0.06] hover:text-[var(--text)]"
          >
            恢复默认
          </button>
        ) : (
          <span className="text-caption text-[var(--text-faint)]">由模型自行决定</span>
        )}
      </div>
      <div className="mt-3 flex items-center justify-between text-caption text-[var(--text-faint)]">
        <span>更快</span>
        <span>更聪明</span>
      </div>
      {/* 轨道：横向，刻度点均布；整条轨道是一个指针目标，按下即选、按住可拖。
          touch-none 让触屏拖动不被当成页面滚动；刻度点与滑块都是纯装饰 */}
      <div
        ref={trackRef}
        role="slider"
        tabIndex={0}
        aria-valuemin={0}
        aria-valuemax={Math.max(stops.length - 1, 0)}
        aria-valuenow={index < 0 ? undefined : index}
        aria-valuetext={currentLabel}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerEnd}
        onPointerCancel={onPointerEnd}
        className="relative mt-2 h-8 cursor-pointer touch-none select-none rounded-full bg-white/[0.06] outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent)]/60"
      >
        {/* 已选段用主色冷银填充（与应用更新进度条同款）：此前只比轨道亮 2%，
            选了几档肉眼看不出；滑块保持纯白，仍是整根轨道上最亮的点 */}
        {index >= 0 && (
          <div
            aria-hidden
            className="absolute inset-y-0 left-0 rounded-full bg-[var(--accent)]/55 transition-[width] duration-100 ease-out"
            style={{ width: stopLeft(index) }}
          />
        )}
        {stops.map((level, i) => (
          <span
            key={level}
            aria-hidden
            title={THINKING_LEVEL_LABELS[level] ?? level}
            className={`pointer-events-none absolute top-1/2 block size-1.5 -translate-x-1/2 -translate-y-1/2 rounded-full ${
              i <= index ? "bg-black/40" : "bg-white/25"
            }`}
            style={{ left: stopLeft(i) }}
          />
        ))}
        {index >= 0 && (
          <span
            aria-hidden
            className="pointer-events-none absolute top-1/2 h-7 w-7 -translate-x-1/2 -translate-y-1/2 rounded-full bg-white shadow-[0_1px_6px_rgba(0,0,0,0.4)] transition-[left] duration-100 ease-out"
            style={{ left: stopLeft(index) }}
          />
        )}
      </div>
    </div>
  );

  return (
    <div ref={rootRef} className="relative shrink-0">
      {panel && createPortal(panel, document.body)}
      <button
        type="button"
        aria-label={`思维链强度：${currentLabel}`}
        aria-haspopup="dialog"
        aria-expanded={open}
        disabled={disabled}
        onClick={toggle}
        title="思维链强度"
        // 与模型 pill 同款的安静样式：选了档位由文案本身体现，不常驻高亮底色
        className={PILL_CLASS}
      >
        {currentLabel}
      </button>
    </div>
  );
}
