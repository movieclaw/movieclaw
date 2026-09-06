"use client";

import { useEffect } from "react";

import { softKeyboardPossible } from "@/lib/soft-keyboard";

/**
 * 软键盘适配的全站收口：撑出键盘占位 + 窗口滚动归位。两件事同源（都由
 * 可视视口的变化驱动），放在一个 effect 里，也保证「先改高度、再归位」的顺序。
 *
 * —— 一、键盘占位（--keyboard-inset）——
 *
 * iOS 弹出软键盘时**不改布局视口**：window.innerHeight 与 100dvh 都不变，
 * 只有 visualViewport.height 变矮。于是 h-[100dvh] 的外壳仍是满屏高，贴在
 * 外壳底部的输入行（Agent 对话页的 Composer）整个落在键盘底下——点一下输入框，
 * 光标就看不见了。CSS 没有任何单位能表达「键盘之上的可视区域」，只能算出来：
 *
 *     键盘占高 = 布局视口高 - 可视视口高
 *
 * 结果写进 :root 的 --keyboard-inset，外壳高度按它收缩（见 app-shell.tsx 的
 * h-[calc(100dvh-var(--keyboard-inset))]），输入行就自然停在键盘上沿。
 * 键盘上方的候选栏 / 第三方输入法工具条已计入 visualViewport.height，无需另算。
 *
 * 这个式子在「键盘改的是布局视口」的浏览器上（部分 Android 形态）自动退化为
 * 0——innerHeight 跟着一起变矮，差值恒为 0，布局本就由 dvh 收好了，不重复让位。
 *
 * 差值只在**焦点确实落在可输入元素上**时才当作键盘（见 lib/soft-keyboard.ts）：
 * iOS 上焦点元素随组件卸载消失时，可视视口经常停在变矮的状态不恢复，光凭差值
 * 会把外壳永久缩着。除视口 resize 外，focusout 与 pointerdown 也重算一次，
 * 保证键盘一收起（哪怕 WebKit 没补发 resize）高度立刻还原。
 *
 * —— 二、窗口滚动归位 ——
 *
 * 全站是「外壳固定 + 内层容器滚动」，窗口本身永远不该有滚动偏移。但为了修
 * 底部黑条，html/body 被撑高了 --vp-overshoot（见 globals.css，两处是一对），
 * 窗口因此多出一截可滚动区域；iOS 弹出软键盘时会滚动窗口去露出聚焦的输入框
 * （这一截远不够露出输入框，纯属白滚），收起后这段偏移**不会还原**——整页
 * 向上错位一截，顶栏叠进状态栏。
 *
 * CSS 治不了（overflow: hidden 挡得住用户滚，挡不住系统发起的滚动），只能滚回去。
 * body 是 overflow: hidden，窗口滚动只可能由系统发起，因此三个信号都无脑归位：
 * - scroll：iOS 为露出聚焦输入框而滚窗口（键盘弹出瞬间，此时外壳还没收缩）；
 * - focusout：点「完成」或点空白处收起键盘（输入框失焦）；
 * - visualViewport resize：下滑手势收起键盘（输入框保持聚焦，只有视口高度变化）。
 *
 * 用户捏合放大时（scale > 1）窗口滚动是合法的浏览能力，此时两件事都不做。
 * 桌面端窗口偏移恒为 0、键盘占高恒为 0，整个 effect 是空转，无需按端分支。
 */
export function ViewportKeyboard() {
  useEffect(() => {
    const root = document.documentElement;
    const vv = window.visualViewport;
    // 捏合放大后可视视口同样变矮，但那不是键盘，一律不介入
    const zoomed = () => (vv ? vv.scale > 1.01 : false);

    let applied = 0;
    const applyInset = () => {
      if (!vv) return;
      const gap = zoomed() || !softKeyboardPossible() ? 0 : Math.round(window.innerHeight - vv.height);
      // 24px 以下按取整误差 / 浏览器自身工具条的收放处理，不当键盘
      const next = gap > 24 ? gap : 0;
      if (next === applied) return;
      applied = next;
      root.style.setProperty("--keyboard-inset", `${next}px`);
    };

    const resetScroll = () => {
      if (zoomed()) return;
      if (window.scrollY !== 0 || window.scrollX !== 0) window.scrollTo(0, 0);
    };

    // 高度同步走同步路径：对话页要在同一拍里按新高度重新贴底（见
    // agent-conversation-view.tsx 的 resize 监听），推迟就会读到旧高度。
    // 归位则推迟一拍——收起动画期间视口高度尚未恢复，此刻判定会跳过，
    // 随后的 resize 会兜住。
    const onResize = () => {
      applyInset();
      window.setTimeout(resetScroll, 0);
    };
    // 推迟一拍等焦点落定（focusout 触发时 activeElement 还没换过去），
    // 再按新焦点重算键盘占高并归位
    const onFocusOut = () =>
      window.setTimeout(() => {
        applyInset();
        resetScroll();
      }, 0);

    window.addEventListener("scroll", resetScroll, { passive: true });
    window.addEventListener("focusout", onFocusOut);
    // 兜底：焦点元素被卸载时 focusout 可能整个不发，界面会一直缩着；
    // 用户下一次触屏就把它纠正回来（无键盘时这里恒等于把占位清零）
    window.addEventListener("pointerdown", applyInset, { passive: true });
    vv?.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("scroll", resetScroll);
      window.removeEventListener("focusout", onFocusOut);
      window.removeEventListener("pointerdown", applyInset);
      vv?.removeEventListener("resize", onResize);
      root.style.removeProperty("--keyboard-inset");
    };
  }, []);
  return null;
}
