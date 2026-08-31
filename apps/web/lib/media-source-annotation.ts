/**
 * 片源人工标注的纯逻辑（docs/design/media-source-annotation.md）。
 *
 * 扫描入库文件的片源常无法从文件名解析（组名不含片源词），同分辨率下洗版
 * 比较不可比，单元停在「无法确认」。标注把用户的整季判断落库，选项与后端
 * 片源档阶梯同源；`user-lowest` 是「不确定，按最低档处理」的显式哨兵（T0），
 * 与系统未知（null，不可比、安静）严格分离。
 */

/** 「不确定，按最低档」哨兵（与后端 movieclaw_matcher.USER_LOWEST_SOURCE 同值） */
export const USER_LOWEST_SOURCE = "user-lowest";

/**
 * 原盘档（T6，与后端 movieclaw_matcher.DISC_SOURCE 同值）。由台账结构自动写入
 * （BDMV / VIDEO_TS / ISO），**不在标注选项里**——用户不需要、也无法把一个
 * 非原盘文件说成原盘。这里只为把它显示成人话。
 */
export const DISC_SOURCE = "Disc";

export type AnnotatableSource =
  | "Remux"
  | "Blu-ray"
  | "WEB-DL"
  | "WEBRip"
  | "HDTV"
  | typeof USER_LOWEST_SOURCE;

export interface MediaSourceOption {
  value: AnnotatableSource;
  label: string;
  /** 选项的后果说明——每个选择都必须是知情选择 */
  consequence: string;
  /** 会触发整季重下的选项，用警示色渲染 */
  destructive?: boolean;
}

export const MEDIA_SOURCE_OPTIONS: MediaSourceOption[] = [
  {
    value: "Remux",
    label: "Remux",
    consequence: "原盘无损重封装，可标注的最高档；达到任何洗版目标，停止洗版",
  },
  {
    value: "Blu-ray",
    label: "蓝光重编码",
    consequence: "蓝光碟压制；高于 WEB-DL 目标，仅低于 Remux 目标",
  },
  {
    value: "WEB-DL",
    label: "WEB-DL",
    consequence: "流媒体原流；洗版目标为 WEB-DL 时即达标停洗",
  },
  {
    value: "WEBRip",
    label: "WEBRip",
    consequence: "流媒体二压；低于 WEB-DL 及以上目标，会自动洗版替换",
  },
  {
    value: "HDTV",
    label: "TV 录制",
    consequence: "电视录制；低于洗版目标，会自动洗版替换",
  },
  {
    value: USER_LOWEST_SOURCE,
    label: "不确定（按最低档）",
    consequence: "按最差处理：整季会重新下载，替换为可证明达标的版本",
    destructive: true,
  },
];

/** 片源值 → 展示名：哨兵值与内部档位值不能直接亮给用户。 */
export function mediaSourceDisplayLabel(value: string | null | undefined): string | null {
  if (!value) return null;
  if (value === USER_LOWEST_SOURCE) return "最低档（人工标注）";
  if (value === DISC_SOURCE) return "原盘";
  return value;
}

/** 洗版报告：存在「无法确认」单元的季号集合（标注入口的显示条件）。 */
export function seasonsNeedingAnnotation(
  units: { season_number: number; state: string }[],
): Set<number> {
  const seasons = new Set<number>();
  for (const u of units) {
    if (u.state === "not_comparable") seasons.add(u.season_number);
  }
  return seasons;
}

/** 追踪明细：存在「无法确认档位」行的季号集合（标注入口的显示条件）。 */
export function seasonsWithIndeterminate(
  wanted: { season_number: number; upgrade?: { indeterminate: boolean } | null }[],
): Set<number> {
  const seasons = new Set<number>();
  for (const w of wanted) {
    if (w.upgrade?.indeterminate) seasons.add(w.season_number);
  }
  return seasons;
}
