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
  /** 正等用户拍板（报错页 / 同意弹窗），见 machine.ts 的 awaitsUserDecision */
  awaitingUser: boolean;
}

/**
 * 控制条必须常显、不能走自动淡出的情况。
 *
 * 前两条都是「用户正要用它」：暂停时他在找播放键；菜单开着时控制条一淡出会
 * 把菜单一起带走（菜单是从控制条里长出来的）。
 *
 * 第三条是「用户必须知道现在什么情况」：报错和同意弹窗都在等他做决定，此时让
 * 界面淡成一块黑屏，他既不知道出了什么事，也不知道还能点什么。**要用户抉择的
 * 状态不设超时**——超时的前提是"没人看也没关系"，这里恰恰相反。
 *
 * **诊断面板不在此列**（曾经在）：它不长在控制条上，是一块独立浮在画面角上的
 * 常驻读数（YouTube 的 Stats for nerds 同款），开着它就把控制条钉死在画面上
 * 只会挡着看片。它自己一直挂到用户点关闭为止，与控制条互不相干。
 */
export function chromeMustStayVisible(input: ChromeVisibilityInput): boolean {
  return input.paused || input.menuOpen || input.awaitingUser;
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
