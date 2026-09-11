/** 会话「最近活跃时间」的刷新口径。
 *
 * 单独成模块是为了能被 node --test 直接覆盖：这里的回归（侧栏列表抖动）
 * 在代码评审里看不出来，只有跑起来才发现。
 */

import type { AgentConversation, AgentTurn } from "./agent-conversations";

/**
 * 用新的 turns 重建会话：running 由轮次派生，``updatedAt`` 只在运行状态翻转
 * （开跑 / 落终态）时刷新一次。
 *
 * 为什么流式增量不算「活跃」：``updatedAt`` 是侧栏「最近会话」的排序键，而
 * 流式产出每 80ms 就合并落一次状态。若每次产出都刷新，同时开着多个会话时，
 * 谁先吐出下一段谁就被顶到列表最前，两个会话每秒能互相换位十几次，整个列表
 * 在跑的时候一直抖。
 *
 * 口径与服务端一致：运行心跳不刷新 ``updated_at``，只有消息追加与运行起止
 * 才算活跃（见 ``movieclaw_db/repositories/agent_session_repo.py``）。本地
 * 跟住服务端，刷新页面前后的排序才不会跳变。
 */
export function applyTurns(
  conversation: AgentConversation,
  turns: AgentTurn[],
  now: number = Date.now(),
): AgentConversation {
  const running = turns.some((turn) => turn.status === "running");
  return {
    ...conversation,
    updatedAt: running === conversation.running ? conversation.updatedAt : now,
    running,
    turns,
  };
}
