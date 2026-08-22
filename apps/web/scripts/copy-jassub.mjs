/**
 * 把 JASSUB（libass WASM）的运行时资产复制到 public/jassub/。
 *
 * 为什么要复制而不是打包：worker 与 wasm 必须以**独立 URL** 交给
 * `new Worker()` 和 `WebAssembly.instantiateStreaming()`，过不了打包器的模块图；
 * 而 Next 的 standalone 产物只带 `.next` 与 `public`，不带 node_modules，
 * 运行时从 node_modules 取文件在容器里必然 404。
 *
 * 为什么不直接把它们提交进仓库：两个 wasm 各 2MB，一个字体 150KB，随依赖
 * 升级还要跟着换——这类二进制留在 node_modules 里由构建期复制，git 历史才干净。
 * 因此 public/jassub/ 进 .gitignore，由 prebuild/predev 钩子保证它总是存在。
 */

import { createRequire } from "node:module";
import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url);
// 从包的 package.json 反推根目录：npm 的扁平布局与 pnpm 的符号链接布局下
// 都能拿到正确路径，不依赖 node_modules 的具体形状。
const root = dirname(require.resolve("jassub/package.json"));

const targetDir = join(import.meta.dirname, "..", "public", "jassub");
mkdirSync(targetDir, { recursive: true });

const assets = [
  // worker 与两份 wasm：modern 版走 SIMD，运行时按浏览器能力二选一
  ["dist/wasm/jassub-worker.js", "jassub-worker.js"],
  ["dist/wasm/jassub-worker.wasm", "jassub-worker.wasm"],
  ["dist/wasm/jassub-worker-modern.wasm", "jassub-worker-modern.wasm"],
  // 兜底字体：字幕指定的字体在本机没有时用它，避免整段字幕渲染不出来
  ["dist/default.woff2", "default.woff2"],
];

for (const [from, to] of assets) {
  copyFileSync(join(root, from), join(targetDir, to));
}

console.log(`已复制 ${assets.length} 个 JASSUB 运行时资产到 public/jassub/`);
