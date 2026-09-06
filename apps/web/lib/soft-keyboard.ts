/**
 * 软键盘是否**可能**正立着——「可视视口变矮 == 键盘占高」这个推断的前置条件。
 *
 * iOS 只用 visualViewport 的高度差表达键盘占高（布局视口纹丝不动），但这个
 * 差值**不保证归零**：焦点元素随组件卸载一起消失时（关掉搜索面板、关掉带
 * 输入框的弹窗），WebKit 常常既不补发 focusout，也不把可视视口恢复到全高。
 * 谁再拿这个差值当键盘高度用，谁就一直缩着不还原：
 * - 外壳按 `100dvh - var(--keyboard-inset)` 缩短，屏幕底部空出一大条，
 *   页面内容被拦腰截断（iOS 独立 App 实测，键盘收起后必现）；
 * - 弹窗容器的 bottom 被抬起，移动端底部抽屉从屏幕**上沿**溢出，
 *   面板标题和关闭按钮跑到状态栏外面，滚也滚不回来。
 *
 * 键盘只可能在可输入元素聚焦时立着（<select> 的滚轮选择器同理，它在 iOS 上
 * 与键盘走同一套视口压缩），这是比事件更硬的判据：焦点不在这类元素上，
 * 一律按「没有键盘」处理，高度差多半是 WebKit 还没恢复的残值。
 *
 * 两个使用方是一对，改动前先看另一处：components/viewport-keyboard.tsx
 * （全站 --keyboard-inset）、components/modal.tsx（弹窗容器抬高）。
 */
export function softKeyboardPossible(): boolean {
  const el = document.activeElement;
  return el?.matches('input,textarea,select,[contenteditable]:not([contenteditable="false"])') ?? false;
}
