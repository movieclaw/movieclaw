/**
 * 播放器状态机（docs/design/web-player.md §6.5「显式状态机」）。
 *
 * 为什么不能用一堆 boolean 拼：起播是「决策 → 开会话 → 缓冲 → 出画」四段
 * 异步，中间随时可能插进来 seek、降档、用户切集。boolean 拼出来的组合里
 * 一定有「正在降档又正在缓冲」这种自相矛盾的中间态，表现是转圈不消失、
 * 或者一次失败触发两条降档链（两个 ffmpeg 一起跑）。
 *
 * 本模块是**纯 reducer**，不碰 DOM 也不发请求——降档回路里最容易出错的
 * 「连败两次要跳兜底档」「已失败的档位不能再被选中」这类判断因此可以直接
 * 用表驱动单测覆盖。
 */

import type { PlaybackDecision, PlaybackSession } from "@/lib/api/playback";

/**
 * 与文档 §6.5 的状态列表一致，多一个 `consent`：软件转码同意链路（§3.6）
 * 是决策的三态之一，界面上是一个必须由用户拍板的停顿，不是错误也不是加载中。
 * 把它挤进 error 会让「点了同意继续播」这条回边无处安放。
 */
export type PlayerPhase =
  | "idle"
  | "deciding"
  | "session-starting"
  | "buffering"
  | "playing"
  | "seeking"
  | "degrading"
  | "consent"
  | "error"
  | "ended";

/**
 * 「等用户拍板」的两个态：报错页与同意弹窗。
 *
 * 它们是**终态**，只有用户自己的动作（重试 / 同意 / 换一集）能离开。挡的是
 * video 元素的迟到事件：解码失败后媒体元素并不安静，`playing`/`ended`/`seeking`
 * 仍可能各自再飞一发，而这些事件当时无条件改写 phase，报错浮层就被它们顶掉了
 * ——用户看到提示闪几秒，然后只剩一个黑播放器，不知道发生了什么、也没得选。
 *
 * 进了这两个态就没有在用的会话（fatal/failed 都把 session 置空），所以此时收到
 * 的播放事件按定义都是上一轮的残响，忽略它们不会丢掉任何真实进展。
 */
export function awaitsUserDecision(phase: PlayerPhase): boolean {
  return phase === "error" || phase === "consent";
}

/** 只有这些阶段仍绑定着一条可接受媒体事件的当前会话。 */
function acceptsMediaEvent(phase: PlayerPhase): boolean {
  return phase === "buffering" || phase === "playing" || phase === "seeking" || phase === "ended";
}

/** 最高兜底档：软件转码。连败两次后直接跳到它，不再逐级试。 */
const FALLBACK_TIER = 4;

/** 连续失败到这个次数就放弃逐级降档（§6.3）。 */
const MAX_STEPWISE_FAILURES = 2;

export interface PlayerState {
  phase: PlayerPhase;
  /** 当前生效的会话（含 stream_url / start_ms / 字幕地址）；档 0 也有，只是 session_id 为 null */
  session: PlaybackSession | null;
  decision: PlaybackDecision | null;
  /** 已经失败过的档位，带给下次 decide，服务端据此跳过它们 */
  failedTiers: number[];
  /** 同一文件连续失败次数 */
  failureCount: number;
  /** 起播位置（文件时间，毫秒）。降档重来要落回同一位置，不能从头放。
   *  **null = 交给服务端按观看状态定**（§6.10 续播并入开会话）；会话响应
   *  回来后被解析出的实际起点覆盖，null 只存在于首次请求在途期间。 */
  startMs: number | null;
  /**
   * 起播尝试序号：每次「用户要求重来」（错误页点重试、同意弹窗点继续）都 +1。
   *
   * 它存在的唯一理由是编排层要给起播 effect 去重——React 严格模式下 effect
   * 会跑两遍，靠状态指纹认出「这是同一次请求」。但重试的本质就是**参数一模
   * 一样再来一次**，没有这个序号，重试算出的指纹与上一次完全相同，会被当成
   * 严格模式的重复调用丢掉：请求发不出去，界面永远停在「正在判断播放方式」。
   * 降档路径不受影响是因为它每次都改 failedTiers/failureCount，指纹天然不同。
   */
  attempt: number;
  error: { message: string; suggestion: string | null } | null;
}

export const initialPlayerState: PlayerState = {
  phase: "idle",
  session: null,
  decision: null,
  failedTiers: [],
  failureCount: 0,
  startMs: 0,
  attempt: 0,
  error: null,
};

export type PlayerEvent =
  /** 用户点了播放 / 切了集：从头走决策流程，清掉上一轮的失败记录。
   *  startMs 为 null = 起点交给服务端（续播点；看完的从头播） */
  | { type: "request"; startMs: number | null }
  /** 决策 + 开会话回来了（三态都走这一条，由 decision.outcome 分流） */
  | { type: "session"; session: PlaybackSession }
  /** 用户在同意弹窗里点了「继续」：重新决策，这次服务端会给出软转计划 */
  | { type: "consent-granted" }
  | { type: "buffering" }
  | { type: "playing" }
  | { type: "seeking" }
  /** seek 越出已转区间：换会话，位置不变 */
  | { type: "restart"; startMs: number }
  /** 播放链路失败（error 事件 / 长时间 stall），触发降档回路 */
  | { type: "failed"; reason: string }
  /** 无法继续（接口报错、会话起不来等），直接进错误态 */
  | { type: "fatal"; message: string; suggestion?: string | null }
  | { type: "ended" }
  | { type: "reset" };

/**
 * 降档：把当前档位记入失败集合，算出下一轮 decide 要跳过哪些档。
 *
 * **逐级降**（1 失败后仍试 2）是实现期定下的取舍：档 2 同样 `-c:v copy`，
 * 若失败原因在视频码流会同样失败，但若原因在音轨，档 2 正好修好。浏览器
 * 给不出可靠的失败归因，多试一档只浪费几秒（一次性），跳过档 2 却可能让
 * 本可直通的视频每次播放都多转一路。
 *
 * 连败两次说明「逐级试」这个假设本身不成立，此时把兜底档以下全标失败，
 * 一步到位。
 */
function nextFailedTiers(state: PlayerState, failedTier: number): number[] {
  const accumulated = new Set(state.failedTiers);
  accumulated.add(failedTier);
  if (state.failureCount + 1 >= MAX_STEPWISE_FAILURES) {
    for (let tier = 0; tier < FALLBACK_TIER; tier += 1) accumulated.add(tier);
  }
  return [...accumulated].sort((a, b) => a - b);
}

export function playerReducer(state: PlayerState, event: PlayerEvent): PlayerState {
  switch (event.type) {
    case "request":
      return {
        ...initialPlayerState,
        phase: "deciding",
        startMs: event.startMs,
        attempt: state.attempt + 1,
      };

    case "session": {
      // 请求已被新的切集/切档动作超越时，旧请求的迟到响应不能复活旧会话。
      if (
        state.phase !== "deciding" &&
        state.phase !== "degrading" &&
        state.phase !== "session-starting"
      ) {
        return state;
      }
      const { decision } = event.session;
      if (decision.outcome === "consent") {
        return { ...state, phase: "consent", decision, session: null };
      }
      if (decision.outcome === "rejected") {
        return {
          ...state,
          phase: "error",
          decision,
          session: null,
          error: { message: decision.reason, suggestion: decision.suggestion },
        };
      }
      return {
        ...state,
        phase: "buffering",
        decision,
        session: event.session,
        startMs: event.session.start_ms,
        error: null,
      };
    }

    case "consent-granted":
      // 开关已经翻开，同一份请求重来一次即可——不清 failedTiers：之前
      // 试过并失败的档位仍然是失败的。
      return { ...state, phase: "deciding", decision: null, attempt: state.attempt + 1 };

    case "buffering":
      // 播放中途的缓冲不该把状态打回起播阶段（会让 UI 闪一下"正在启动"），
      // 只有已经出过画之后才认这条。
      return state.phase === "playing" || state.phase === "seeking"
        ? { ...state, phase: "buffering" }
        : state;

    case "playing":
      if (!acceptsMediaEvent(state.phase)) return state;
      // 出画即认为本档可用：清掉连败计数，之后再失败重新从逐级降开始。
      return { ...state, phase: "playing", failureCount: 0, error: null };

    case "seeking":
      if (awaitsUserDecision(state.phase)) return state;
      // 只有「已经出画」之后的 seek 才算 seeking；起播路上（buffering /
      // session-starting）的那次续播点跳转不能把 phase 顶成 seeking——
      // seeking 不算 busy（正常拖拽不该弹全屏转圈），被它顶掉的后果是
      // 加载转圈提前消失、中央播放键在还放不动的时候就亮出来：用户点了
      // 没反应，以为播放器坏了（真机复现的起播假死感）。ended 之后往回
      // 拖是真 seek，照常进。
      return state.phase === "playing" || state.phase === "seeking" || state.phase === "ended"
        ? { ...state, phase: "seeking" }
        : state;

    case "restart":
      // 先摘掉旧会话，让引擎/会话释放 effect 立即执行；否则旧流的迟到事件会
      // 污染新一轮起播，服务端也会在新会话启动期间继续保留旧 ffmpeg。
      // restart 仍保留 failedTiers：它用于网络自愈与 seek 重开，不代表当前档
      // 已经因为解码失败。
      return {
        ...state,
        phase: "session-starting",
        session: null,
        decision: null,
        startMs: event.startMs,
        error: null,
        // restart 也是一次新的起播尝试：位置不变时若不递增 attempt，会撞上
        // 起播 effect 的去重指纹，第二次请求就发不出去。
        attempt: state.attempt + 1,
      };

    case "failed": {
      // 解码/停滞错误只能由当前会话产生。切档或重开后，旧引擎可能还会发一
      // 个迟到错误；没有这层不变式，它会把没有 decision 的新一轮直接顶成 error。
      if (
        !acceptsMediaEvent(state.phase) ||
        state.session === null ||
        state.decision === null
      ) {
        return state;
      }
      const tier = state.decision?.tier;
      // 还没定档就失败（会话都没起来）：没有可降的档，直接错误态
      if (tier === null || tier === undefined) {
        return {
          ...state,
          phase: "error",
          error: { message: event.reason, suggestion: null },
        };
      }
      const failedTiers = nextFailedTiers(state, tier);
      // 兜底档本身都失败了：没有更低的档可退，只能报错并建议换播放器。
      // session 必须一并置空（与 fatal/degrading 同一不变式，见
      // awaitsUserDecision 的注释）：留着的话心跳还在给这个会话续命、
      // 引擎还挂着在拉流——用户对着错误页，服务端的软转 ffmpeg 却一直
      // 烧着 CPU，正是「播放都停了 ffmpeg 还在跑」的一条来路。
      if (tier >= FALLBACK_TIER) {
        return {
          ...state,
          phase: "error",
          session: null,
          failedTiers,
          failureCount: state.failureCount + 1,
          error: {
            message: event.reason,
            suggestion: "这个文件在浏览器里放不出来，建议用 Infuse、VidHub 等原生播放器打开。",
          },
        };
      }
      return {
        ...state,
        phase: "degrading",
        failedTiers,
        failureCount: state.failureCount + 1,
        session: null,
      };
    }

    case "fatal":
      return {
        ...state,
        phase: "error",
        session: null,
        error: { message: event.message, suggestion: event.suggestion ?? null },
      };

    case "ended":
      if (!acceptsMediaEvent(state.phase)) return state;
      return { ...state, phase: "ended" };

    case "reset":
      return initialPlayerState;

    default:
      return state;
  }
}

/** 转圈该不该显示：起播四段与降档重来都算「还没出画」。 */
export function isBusy(phase: PlayerPhase): boolean {
  return (
    phase === "deciding" ||
    phase === "session-starting" ||
    phase === "buffering" ||
    phase === "degrading"
  );
}
