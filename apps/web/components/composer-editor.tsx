"use client";

import {
  forwardRef,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { LexicalComposer } from "@lexical/react/LexicalComposer";
import { useLexicalComposerContext } from "@lexical/react/LexicalComposerContext";
import { ContentEditable } from "@lexical/react/LexicalContentEditable";
import { LexicalErrorBoundary } from "@lexical/react/LexicalErrorBoundary";
import { OnChangePlugin } from "@lexical/react/LexicalOnChangePlugin";
import { PlainTextPlugin } from "@lexical/react/LexicalPlainTextPlugin";
import {
  LexicalTypeaheadMenuPlugin,
  MenuOption,
  type TriggerFn,
} from "@lexical/react/LexicalTypeaheadMenuPlugin";
import {
  $createLineBreakNode,
  $createParagraphNode,
  $createTextNode,
  $getRoot,
  $getSelection,
  $isRangeSelection,
  COMMAND_PRIORITY_CRITICAL,
  COMMAND_PRIORITY_HIGH,
  KEY_ENTER_COMMAND,
  LineBreakNode,
  PASTE_COMMAND,
  TextNode,
  type EditorConfig,
  type LexicalEditor,
  type LexicalNode,
  type NodeKey,
  type SerializedTextNode,
  type Spread,
} from "lexical";

import { SKILL_TOKEN_SOURCE } from "@/lib/agent-skills";
import { listSkills, type AgentSkill } from "@/lib/api/agent";

/*
 * Lexical 版输入区（docs/design/agent-skills.md §9.2 的富文本升级）。
 *
 * 对 Composer 的契约保持纯字符串：对外 value/onChange 仍是含
 * `/skill:名字 ` 占位符的普通文本（服务端展开逻辑零改动）；编辑器内部把
 * 占位符呈现为 SkillTokenNode——带样式的原子 chip（整体删除、不可拆改）。
 * 技能进入编辑器的两条路：输入「/」触发快捷菜单（Lexical typeahead），
 * 或加号菜单经 ref.insertSkill 插入——殊途同归都是 token 节点。
 *
 * 选 Lexical 的原因：Meta 开源、核心 + react 绑定足够轻、节点模型天然
 * 支持 mention/token 这类实体，后续扩展（@提及媒体库条目、附件内联卡片、
 * 粘贴富内容）都在同一套节点体系里做。
 */

// ── SkillTokenNode：技能占位符的原子 chip 节点 ─────────────────────────────

type SerializedSkillTokenNode = Spread<{ skillName: string }, SerializedTextNode>;

export class SkillTokenNode extends TextNode {
  __skillName: string;

  static getType(): string {
    return "skill-token";
  }

  static clone(node: SkillTokenNode): SkillTokenNode {
    return new SkillTokenNode(node.__skillName, node.__key);
  }

  constructor(skillName: string, key?: NodeKey) {
    // 展示文本带 ⚡ 前缀；真实语义存 __skillName，序列化时还原为占位符
    super(`⚡ ${skillName}`, key);
    this.__skillName = skillName;
  }

  getSkillName(): string {
    return this.__skillName;
  }

  createDOM(config: EditorConfig): HTMLElement {
    const dom = super.createDOM(config);
    dom.className = "skill-token-chip";
    dom.setAttribute("data-skill", this.__skillName);
    return dom;
  }

  static importJSON(serialized: SerializedSkillTokenNode): SkillTokenNode {
    return $createSkillTokenNode(serialized.skillName);
  }

  exportJSON(): SerializedSkillTokenNode {
    return { ...super.exportJSON(), type: "skill-token", skillName: this.__skillName, version: 1 };
  }

  // 原子实体：光标不能进入内部续写，删除时整体消失（token mode 补全该语义）
  canInsertTextBefore(): boolean {
    return false;
  }

  canInsertTextAfter(): boolean {
    return false;
  }

  isTextEntity(): true {
    return true;
  }
}

export function $createSkillTokenNode(skillName: string): SkillTokenNode {
  const node = new SkillTokenNode(skillName);
  node.setMode("token");
  return node;
}

// ── 纯文本 ⇄ 节点树（对外契约的两个方向） ──────────────────────────────────

/** 编辑器内容序列化为普通文本：token 节点还原成 /skill:名字 占位符。 */
function $serializeToText(): string {
  const parts: string[] = [];
  const walk = (node: LexicalNode): void => {
    if (node instanceof SkillTokenNode) {
      parts.push(`/skill:${node.getSkillName()}`);
    } else if (node instanceof LineBreakNode) {
      parts.push("\n");
    } else if (node instanceof TextNode) {
      parts.push(node.getTextContent());
    } else {
      const children = "getChildren" in node ? (node as never as { getChildren(): LexicalNode[] }).getChildren() : [];
      children.forEach(walk);
    }
  };
  $getRoot()
    .getChildren()
    .forEach((child, index) => {
      if (index > 0) parts.push("\n");
      walk(child);
    });
  return parts.join("");
}

/** 用普通文本重建编辑器内容：/skill:名字 占位符还原成 token chip。 */
function $replaceFromText(text: string): void {
  const root = $getRoot();
  root.clear();
  const paragraph = $createParagraphNode();
  const tokenRe = new RegExp(SKILL_TOKEN_SOURCE, "g");
  text.split("\n").forEach((line, lineIndex) => {
    if (lineIndex > 0) paragraph.append($createLineBreakNode());
    let cursor = 0;
    tokenRe.lastIndex = 0;
    for (const match of line.matchAll(tokenRe)) {
      const start = match.index ?? 0;
      if (start > cursor) paragraph.append($createTextNode(line.slice(cursor, start)));
      paragraph.append($createSkillTokenNode(match[1]));
      cursor = start + match[0].length;
    }
    if (cursor < line.length) paragraph.append($createTextNode(line.slice(cursor)));
  });
  root.append(paragraph);
  paragraph.selectEnd();
}

// ── 内部插件 ──────────────────────────────────────────────────────────────

/** 把编辑器实例交给外层 ref（Composer 的加号菜单要调 insertSkill）。 */
function EditorRefPlugin({ editorRef }: { editorRef: { current: LexicalEditor | null } }) {
  const [editor] = useLexicalComposerContext();
  editorRef.current = editor;
  return null;
}

/** 外部 value 变化（提交清空 / 改写重问预填）时重建内容；内部输入不回灌。 */
function ValueSyncPlugin({
  value,
  lastEmitted,
}: {
  value: string;
  lastEmitted: { current: string };
}) {
  const [editor] = useLexicalComposerContext();
  useEffect(() => {
    if (value === lastEmitted.current) return;
    lastEmitted.current = value;
    editor.update(() => $replaceFromText(value));
  }, [editor, value, lastEmitted]);
  return null;
}

/** 回车提交、Shift+回车换行（输入法组合与技能菜单展开时不拦）。 */
function SubmitPlugin({ onSubmit }: { onSubmit: () => void }) {
  const [editor] = useLexicalComposerContext();
  const onSubmitRef = useRef(onSubmit);
  onSubmitRef.current = onSubmit;
  useEffect(
    () =>
      editor.registerCommand(
        KEY_ENTER_COMMAND,
        (event) => {
          if (event === null || event.shiftKey || event.isComposing) return false;
          event.preventDefault();
          onSubmitRef.current();
          return true;
        },
        // HIGH：低于技能菜单的 CRITICAL（菜单展开时回车是选中项，不是发送）
        COMMAND_PRIORITY_HIGH,
      ),
    [editor],
  );
  return null;
}

/** 粘贴里带文件（截图）时交给附件链路，纯文本粘贴走默认行为。 */
function PasteFilesPlugin({ onFiles }: { onFiles: (files: FileList) => void }) {
  const [editor] = useLexicalComposerContext();
  const onFilesRef = useRef(onFiles);
  onFilesRef.current = onFiles;
  useEffect(
    () =>
      editor.registerCommand(
        PASTE_COMMAND,
        (event) => {
          const files = event instanceof ClipboardEvent ? event.clipboardData?.files : null;
          if (files && files.length > 0) {
            event.preventDefault();
            onFilesRef.current(files);
            return true;
          }
          return false;
        },
        COMMAND_PRIORITY_HIGH,
      ),
    [editor],
  );
  return null;
}

function EditablePlugin({ disabled }: { disabled: boolean }) {
  const [editor] = useLexicalComposerContext();
  useEffect(() => editor.setEditable(!disabled), [editor, disabled]);
  return null;
}

function AutoFocusPlugin() {
  const [editor] = useLexicalComposerContext();
  useEffect(() => editor.focus(), [editor]);
  return null;
}

// ── 斜杠快捷菜单 ──────────────────────────────────────────────────────────

class SkillMenuOption extends MenuOption {
  skill: AgentSkill;

  constructor(skill: AgentSkill) {
    super(skill.name);
    this.skill = skill;
  }
}

/** 触发规则：行首或空白后敲「/」即开菜单，后续字符收窄匹配。
 * 不用 useBasicTypeaheadTriggerMatch——其默认 punctuation 会在技能名的
 * 连字符处截断查询。 */
const slashTrigger: TriggerFn = (text) => {
  const match = /(^|\s)(\/([A-Za-z0-9._-]{0,64}))$/.exec(text);
  if (match === null) return null;
  return {
    leadOffset: match.index + match[1].length,
    matchingString: match[3],
    replaceableString: match[2],
  };
};

function SkillTypeaheadPlugin() {
  const [editor] = useLexicalComposerContext();
  const [skills, setSkills] = useState<AgentSkill[]>([]);
  const [query, setQuery] = useState<string | null>(null);

  // 打开时现拉（与加号菜单同口径：改技能即生效，不做跨次缓存）
  const handleOpen = useCallback(() => {
    listSkills()
      .then(setSkills)
      .catch(() => setSkills([]));
  }, []);

  const options = useMemo(() => {
    const q = (query ?? "").toLowerCase();
    // 「/skill:」前缀视为手敲完整占位符的开头，按名字部分过滤
    const bare = q.startsWith("skill:") ? q.slice(6) : q;
    return skills
      .filter(
        (skill) =>
          bare.length === 0 ||
          skill.name.toLowerCase().includes(bare) ||
          skill.description.toLowerCase().includes(bare),
      )
      .slice(0, 8)
      .map((skill) => new SkillMenuOption(skill));
  }, [skills, query]);

  const onSelect = useCallback(
    (option: SkillMenuOption, nodeToReplace: TextNode | null, closeMenu: () => void) => {
      editor.update(() => {
        const token = $createSkillTokenNode(option.skill.name);
        if (nodeToReplace) {
          nodeToReplace.replace(token);
        } else {
          $getSelection()?.insertNodes([token]);
        }
        const space = $createTextNode(" ");
        token.insertAfter(space);
        space.select(1, 1);
      });
      closeMenu();
    },
    [editor],
  );

  return (
    <LexicalTypeaheadMenuPlugin<SkillMenuOption>
      onQueryChange={setQuery}
      onSelectOption={onSelect}
      onOpen={handleOpen}
      options={options}
      triggerFn={slashTrigger}
      commandPriority={COMMAND_PRIORITY_CRITICAL}
      menuRenderFn={(anchorRef, { selectedIndex, selectOptionAndCleanUp, setHighlightedIndex }) =>
        anchorRef.current && options.length > 0
          ? createPortal(
              <div
                role="listbox"
                aria-label="技能快捷选择"
                className="menu-surface absolute bottom-[1.8em] left-0 z-50 max-h-64 w-72 overflow-y-auto p-1.5"
              >
                <p className="px-2.5 pb-0.5 pt-1 text-caption text-[var(--text-faint)]">使用技能</p>
                {options.map((option, index) => (
                  <button
                    key={option.key}
                    type="button"
                    role="option"
                    aria-selected={selectedIndex === index}
                    onMouseEnter={() => setHighlightedIndex(index)}
                    onClick={() => selectOptionAndCleanUp(option)}
                    className={`block w-full rounded-[10px] px-2.5 py-1.5 text-left transition-colors ${
                      selectedIndex === index ? "bg-white/[0.08]" : "hover:bg-white/[0.06]"
                    }`}
                  >
                    <span className="block text-ui text-[var(--text)]">⚡ {option.skill.name}</span>
                    <span className="block truncate text-caption text-[var(--text-muted)]">
                      {option.skill.description}
                    </span>
                  </button>
                ))}
              </div>,
              anchorRef.current,
            )
          : null
      }
    />
  );
}

// ── 对外组件 ──────────────────────────────────────────────────────────────

export interface ComposerEditorHandle {
  /** 在当前光标处（无光标则末尾）插入技能 token；加号菜单用 */
  insertSkill: (name: string) => void;
  focus: () => void;
}

export interface ComposerEditorProps {
  value: string;
  onChange: (text: string) => void;
  onSubmit: () => void;
  placeholder: string;
  disabled: boolean;
  autoFocus: boolean;
  /** 开启「/」技能快捷菜单 */
  skillPicker: boolean;
  /** 粘贴图片文件时的回调；不传则粘贴文件走浏览器默认行为 */
  onPasteFiles?: (files: FileList) => void;
}

export const ComposerEditor = forwardRef<ComposerEditorHandle, ComposerEditorProps>(
  function ComposerEditor(
    { value, onChange, onSubmit, placeholder, disabled, autoFocus, skillPicker, onPasteFiles },
    ref,
  ) {
    const editorRef = useRef<LexicalEditor | null>(null);
    // 最近一次向外发出的序列化文本：区分「内部输入」与「外部改值」
    const lastEmitted = useRef(value);

    const initialConfig = useMemo(
      () => ({
        namespace: "movieclaw-composer",
        nodes: [SkillTokenNode],
        editable: !disabled,
        onError: (error: Error) => {
          throw error;
        },
        editorState: value
          ? () => $replaceFromText(value)
          : undefined,
      }),
      // 初始配置只在挂载时消费，后续变化由各插件同步
      // eslint-disable-next-line react-hooks/exhaustive-deps
      [],
    );

    useImperativeHandle(
      ref,
      () => ({
        insertSkill: (name: string) => {
          const editor = editorRef.current;
          if (!editor) return;
          editor.update(() => {
            const token = $createSkillTokenNode(name);
            const space = $createTextNode(" ");
            const selection = $getSelection();
            if ($isRangeSelection(selection)) {
              // token 序列化后必须处在行首或空白之后（前后端 SKILL_TOKEN_RE
              // 同款前置断言），紧贴文字插入时先补一个空格，否则服务端不展开
              let needsLeadingSpace = false;
              const anchor = selection.anchor;
              if (selection.isCollapsed() && anchor.type === "text") {
                const anchorNode = anchor.getNode();
                const before =
                  anchor.offset > 0
                    ? anchorNode.getTextContent().slice(anchor.offset - 1, anchor.offset)
                    : (anchorNode.getPreviousSibling()?.getTextContent().slice(-1) ?? "");
                needsLeadingSpace = before !== "" && !/\s/.test(before);
              }
              selection.insertNodes(
                needsLeadingSpace ? [$createTextNode(" "), token, space] : [token, space],
              );
            } else {
              const paragraph = $getRoot().getLastChild() ?? $createParagraphNode();
              if (paragraph.getParent() === null) $getRoot().append(paragraph);
              const tail = paragraph.getTextContent();
              const nodes: LexicalNode[] =
                tail !== "" && !/\s$/.test(tail) ? [$createTextNode(" "), token, space] : [token, space];
              (paragraph as never as { append(...nodes: LexicalNode[]): void }).append(...nodes);
            }
            space.select(1, 1);
          });
          editor.focus();
        },
        focus: () => editorRef.current?.focus(),
      }),
      [],
    );

    return (
      <LexicalComposer initialConfig={initialConfig}>
        <div className="relative">
          <PlainTextPlugin
            contentEditable={
              <ContentEditable
                aria-label="消息输入框"
                className="scroll-thin block h-[66px] w-full overflow-y-auto whitespace-pre-wrap bg-transparent px-4 pb-1 pt-3.5 text-body leading-6 text-[var(--text)] focus:outline-none"
              />
            }
            placeholder={
              <div
                aria-hidden
                className={`pointer-events-none absolute left-4 top-3.5 select-none text-body leading-6 ${
                  disabled ? "text-[var(--text-muted)]" : "text-[var(--text-faint)]"
                }`}
              >
                {placeholder}
              </div>
            }
            ErrorBoundary={LexicalErrorBoundary}
          />
        </div>
        <EditorRefPlugin editorRef={editorRef} />
        <OnChangePlugin
          ignoreSelectionChange
          onChange={(editorState) => {
            const text = editorState.read($serializeToText);
            if (text === lastEmitted.current) return;
            lastEmitted.current = text;
            onChange(text);
          }}
        />
        <ValueSyncPlugin value={value} lastEmitted={lastEmitted} />
        <SubmitPlugin onSubmit={onSubmit} />
        <EditablePlugin disabled={disabled} />
        {autoFocus && !disabled && <AutoFocusPlugin />}
        {onPasteFiles && <PasteFilesPlugin onFiles={onPasteFiles} />}
        {skillPicker && <SkillTypeaheadPlugin />}
      </LexicalComposer>
    );
  },
);
