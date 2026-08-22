/**
 * 控制条显隐规则（docs/design/web-player.md §6.8）。
 *
 * 抽成纯函数是被一个线上 bug 逼出来的：`pointerleave` 在**触屏上每次点击都会
 * 触发**（实测事件序列是 `pointerdown → pointerup → pointerleave → click`），
 * 而它当时被直接接到「收起控制条」上——于是手机上每点一下，控制条刚露头就被
 * 自己收掉，表现为「闪一下全没了」，而且永远没法让它停留。鼠标上则完全不会
 * 触发 pointerleave，所以桌面开发时一次也复现不到。
 *
 * 这类「哪个指针类型该走哪条路」的判断散在 JSX 的行内箭头函数里就没法测，
 * 所以放这里。
 */

export interface ChromeVisibilityInput {
  /** 播放器暂停中 */
  paused: boolean;
  /** 有菜单展开着（字幕 / 设置） */
  menuOpen: boolean;
  /** 诊断面板开着 */
  diagnosticsOpen: boolean;
}

/**
 * 控制条必须常显、不能走自动淡出的情况。
 *
 * 三条都是「用户正要用它」：暂停时他在找播放键；菜单或诊断面板开着时，控制条
 * 一淡出会把它们一起带走。
 */
export function chromeMustStayVisible(input: ChromeVisibilityInput): boolean {
  return input.paused || input.menuOpen || input.diagnosticsOpen;
}

export interface PointerLeaveInput {
  /** `PointerEvent.pointerType`：mouse / touch / pen */
  pointerType: string;
  paused: boolean;
}

/**
 * 指针离开播放器时该不该立刻收起控制条。
 *
 * **只认鼠标**。「指针离开了播放器区域」这件事只对鼠标成立——手指抬起来就算
 * 「离开」，把它当成「用户不看了」是错的。笔（pen）同理：抬笔不等于离开。
 */
export function shouldHideOnPointerLeave(input: PointerLeaveInput): boolean {
  if (input.pointerType !== "mouse") return false;
  // 暂停时本来就要常显，见 chromeMustStayVisible
  return !input.paused;
}
