/**
 * 画中画的能力判定。
 *
 * 看着只是「两个标志取个或」，但**取或是错的**，这条规则由 iOS 主屏 Web App
 * （PWA，加到主屏后的独立形态）定死：
 *
 * 那个形态里 WebKit 根本不给网页画中画的通路，而两个信号一个撒谎一个诚实——
 *
 * | 信号 | Safari | iOS 主屏 Web App |
 * |------|--------|------------------|
 * | `document.pictureInPictureEnabled` | true | **true（假阳性）** |
 * | `webkitSupportsPresentationMode("picture-in-picture")` | true | **false（准）** |
 *
 * 取或的话诚实的那个永远救不了场：按钮照渲染，点下去 `requestPictureInPicture()`
 * 无声失败，用户看到一颗死键。所以**前缀 API 在就以它为准**（Apple 平台上只有
 * 它准），没有才退回标准标志（Chrome/Firefox 这些压根没有前缀 API 的浏览器）。
 *
 * 这也是 WebKit bug 303885 里建议的做法。那个 bug 2025-12 报出、P1，到 2026-08
 * 仍未修复，最近一次复现在 iOS 26.6（Apple 内部单号 rdar://121964774）——也就是
 * 说 iOS 主屏 Web App 里的画中画目前**网页侧无解**，能做的只有别给假按钮。
 */
export interface PipEnvironment {
  /** `video.disablePictureInPicture`：视频自己声明不许进小窗 */
  disabled: boolean;
  /**
   * `video.webkitSupportsPresentationMode("picture-in-picture")` 的结果；
   * **null 表示压根没有这个 API**（非 Apple 浏览器），不是「探测为假」。
   */
  webkitSupports: boolean | null;
  /** `document.pictureInPictureEnabled` */
  standardEnabled: boolean;
}

export function pipSupported({ disabled, webkitSupports, standardEnabled }: PipEnvironment): boolean {
  // 一票否决：视频声明了不许，两套 API 都会拒
  if (disabled) return false;
  // Apple 平台：前缀 API 是唯一可信的那个
  if (webkitSupports !== null) return webkitSupports;
  return standardEnabled;
}
