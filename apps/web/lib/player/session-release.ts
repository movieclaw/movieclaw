/**
 * 转码会话的「离开即释放」。
 *
 * 为什么需要它：`pagehide` 只在真正卸载文档时触发，**SPA 内部返回媒体库、
 * 切下一集都不会触发**。光靠服务端的心跳超时回收（三分钟），那段时间里一个没人看
 * 的 ffmpeg 还在烧 CPU、写盘，并且占着并发名额——默认转码并发只有 2，开两个
 * 片再离开，第三次播放就会被「转码会话已满（2/2）」挡住，而实际什么都没在播。
 *
 * 为什么不能直接在 effect 的 cleanup 里停：React StrictMode 下 effect 会
 * mount→cleanup→mount 跑两遍，直接停会把刚起的会话停掉，开发模式必炸。
 * 因此 release 只是**预约**一次停止，真正执行时再比对当前占用者——若同一个
 * 会话已经被重新 acquire（StrictMode 重挂），这次停止就作废。
 *
 * 调度器可注入，测试因此不必依赖真实计时器。
 */

export interface SessionReleaser {
  /** 登记当前正在使用的会话。 */
  acquire(sessionId: string): void;
  /** 预约释放。真正停止发生在下一拍，且仅当该会话届时确实不再被占用。 */
  release(sessionId: string): void;
}

export function createSessionReleaser(
  stop: (sessionId: string) => void,
  schedule: (task: () => void) => void = (task) => {
    setTimeout(task, 0);
  },
): SessionReleaser {
  let active: string | null = null;
  return {
    acquire(sessionId: string) {
      active = sessionId;
    },
    release(sessionId: string) {
      // 只让出自己占的位置：并发换会话时，新会话可能已经 acquire 过了
      if (active === sessionId) active = null;
      schedule(() => {
        if (active === sessionId) return; // 又被挂回来了（StrictMode 重挂）
        stop(sessionId);
      });
    },
  };
}
