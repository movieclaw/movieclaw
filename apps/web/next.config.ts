import type { NextConfig } from "next";

function trimTrailingSlash(value: string): string {
  return value !== "/" && value.endsWith("/") ? value.slice(0, -1) : value;
}

const apiBaseUrl = trimTrailingSlash(process.env.NEXT_PUBLIC_API_BASE_URL?.trim() || "/api/v1");
const proxyTarget = trimTrailingSlash(process.env.MOVIECLAW_API_PROXY_TARGET?.trim() || "http://127.0.0.1:8000");

const nextConfig: NextConfig = {
  // 构建目录可用环境变量覆盖：并行的第二个 dev server（如会话内浏览器预览）
  // 必须使用独立目录，两个 next dev 同写 .next 会互相损坏 chunk。
  distDir: process.env.NEXT_DIST_DIR?.trim() || ".next",
  // Docker 部署用 standalone 输出：只带被引用到的依赖，镜像里无需完整 node_modules。
  output: "standalone",
  // 关闭 Next 图片优化：站内 next/image 只用于静态 logo，优化收益为零；
  // 关闭后 standalone 产物不再依赖 sharp 原生模块，前端构建产物跨 CPU 架构通用
  // （Docker 交叉构建时前端可在宿主架构原生编译，不必走 QEMU 模拟）。
  images: { unoptimized: true },
  // 构建并发上限（仅 Docker 构建设置此变量）：Next 默认按 CPU 核数开静态生成
  // worker，而 Docker 虚拟机往往是「核多内存少」（如 12 核 / 8G）。页面数量
  // 长上来后 worker 一起吃内存，构建会静默挂死——日志停在 "Creating an
  // optimized production build"、CPU 掉到接近 0。限并发是这个现象的根治手段。
  ...(process.env.NEXT_BUILD_CPUS
    ? { experimental: { cpus: Number(process.env.NEXT_BUILD_CPUS) } }
    : {}),
  reactStrictMode: true,
  typedRoutes: true,
  // 关闭左下角 Next.js 开发指示器（dev tools 浮动按钮）
  devIndicators: false,
  async rewrites() {
    // API 走同源路径时，由 Next 服务器反代到后端。开发和生产（单容器部署，
    // 前端进程反代到同容器内 127.0.0.1:8000 的后端）都依赖这条规则，
    // 因此不再按 NODE_ENV 区分。反代目标在构建时通过 MOVIECLAW_API_PROXY_TARGET 固化。
    if (!apiBaseUrl.startsWith("/")) {
      return [];
    }

    // Jellyfin 兼容播放接口的命名空间（docs/design/jellyfin-compat.md 8.3）：
    // 播放器直连本前端端口，这些前缀反代到后端根路径。大小写两种形态都注册
    // （Next 匹配大小写敏感；后端另有归一化中间件兜底）。
    const jellyfinNamespaces = [
      "System",
      "Users",
      "UserViews",
      "UserItems",
      "UserPlayedItems",
      "UserFavoriteItems",
      "Items",
      "Videos",
      "Shows",
      "Sessions",
      "PlayingItems",
      "Branding",
      "QuickConnect",
      "DisplayPreferences",
      "emby",
    ];
    const jellyfinRewrites = [
      ...new Set(jellyfinNamespaces.flatMap((ns) => [ns, ns.toLowerCase()])),
    ].map((ns) => ({
      source: `/${ns}/:path*`,
      destination: `${proxyTarget}/${ns}/:path*`,
    }));
    // Library 命名空间不能整段通配：Next 的 rewrite source 匹配大小写不敏感，
    // `/Library/:path*` 会连带劫持本应用自己的媒体库页面 /library/[id]。
    // 只反代真 Jellyfin 在该命名空间下的字面 API 子路径（LibraryController.cs /
    // LibraryStructureController.cs），与页面的数字 id 段互不相交。
    for (const sub of ["VirtualFolders", "MediaFolders", "PhysicalPaths", "Refresh"]) {
      jellyfinRewrites.push({
        source: `/Library/${sub}/:path*`,
        destination: `${proxyTarget}/Library/${sub}/:path*`,
      });
      jellyfinRewrites.push({
        source: `/Library/${sub}`,
        destination: `${proxyTarget}/Library/${sub}`,
      });
    }

    return [
      {
        source: `${apiBaseUrl}/:path*`,
        destination: `${proxyTarget}${apiBaseUrl}/:path*`,
      },
      ...jellyfinRewrites,
    ];
  },
};

export default nextConfig;
