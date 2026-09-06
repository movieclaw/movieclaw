/**
 * 对话框「模型 / 思维链」最近一次的选择（本浏览器记忆，localStorage）。
 *
 * 会话里的选择随消息写进转录信封、由服务端沿用，那是会话自己的状态；但
 * 新任务页没有历史可沿用，每次都从「默认」起步，用户换个模型、调个强度
 * 得在每个新任务上重做一遍。这里在用户每次改选择器时立刻记下，新任务页
 * 以它为起点——与播放器画质、活动页范围口径同一套「记在本浏览器」的做法。
 *
 * 记忆不是事实源：模型清单会变（删了实例、改了目录），读回来的引用必须对着
 * 已加载的清单校验（reconcileComposerPrefs），失效的直接丢弃、不猜近似。
 */

export interface ComposerPrefs {
  /** 模型引用（选项的 ref）；null = 默认模型 */
  model: string | null;
  /** 思维链档位；null = 模型默认 */
  thinking: string | null;
}

/** 校验记忆时只需要清单的这三样，避免把 API 类型拖进纯模块 */
export interface ComposerPrefsModelOption {
  ref: string;
  is_default: boolean;
  thinking_levels?: string[];
}

const STORAGE_KEY = "movieclaw.composer.choice";

const EMPTY: ComposerPrefs = { model: null, thinking: null };

/** 读上次的选择。没存过、存的内容不合法、或没有 localStorage 都回「默认 / 默认」。 */
export function loadComposerPrefs(): ComposerPrefs {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return EMPTY;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return EMPTY;
    const { model, thinking } = parsed as Record<string, unknown>;
    return {
      model: typeof model === "string" && model ? model : null,
      thinking: typeof thinking === "string" && thinking ? thinking : null,
    };
  } catch {
    return EMPTY;
  }
}

/** 记下这次的选择；两项都是默认时直接清掉键。写不进（隐私模式）只是不记住。 */
export function saveComposerPrefs(prefs: ComposerPrefs): void {
  try {
    if (prefs.model === null && prefs.thinking === null) {
      window.localStorage.removeItem(STORAGE_KEY);
    } else {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(prefs));
    }
  } catch {
    // 忽略写入失败
  }
}

/**
 * 对着已加载的模型清单校验记忆：
 * - 清单还没回来（空）时原样返回，不能把记忆当失效清掉；
 * - 记的模型不在清单里 → 模型与档位一起丢弃（档位是跟着模型的）；
 * - 档位不在生效模型（记的模型，或默认模型）的菜单里 → 只丢档位。
 */
export function reconcileComposerPrefs(
  prefs: ComposerPrefs,
  options: readonly ComposerPrefsModelOption[],
): ComposerPrefs {
  if (options.length === 0) return prefs;
  const model = prefs.model;
  let effective: ComposerPrefsModelOption | undefined;
  if (model !== null) {
    effective = options.find((o) => o.ref === model);
    if (!effective) return EMPTY;
  } else {
    effective = options.find((o) => o.is_default) ?? options[0];
  }
  const thinking =
    prefs.thinking !== null && effective.thinking_levels?.includes(prefs.thinking)
      ? prefs.thinking
      : null;
  return model === prefs.model && thinking === prefs.thinking ? prefs : { model, thinking };
}
