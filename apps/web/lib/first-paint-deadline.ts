/**
 * 首屏预算：一条慢接口不该把整页押住。
 *
 * 媒体库首页要等库列表与合集列表都回来才画第一帧。合集列表是首页最重的一条读，
 * 后台任务压着数据库时曾慢到十几秒——页面就一直「正在加载媒体库」，而库列表
 * 几十毫秒就回来了。这里给次要数据一个预算：预算内回来照常用；超时先拿兜底值
 * 把页面画出来，真正的结果晚到时交给 `late` 补上。
 *
 * 失败与超时同一口径：都退回兜底值。要区分的话上游自己 catch。
 */
export interface DeadlineOutcome<T> {
  /** 预算内拿到的结果；超时或失败时是 `fallback` */
  value: T;
  /** 超时了：晚到的那份（失败也归成 `fallback`，永不 reject）；预算内已定则为 null */
  late: Promise<T> | null;
}

export function withDeadline<T>(
  pending: Promise<T>,
  budgetMs: number | null,
  fallback: T,
): Promise<DeadlineOutcome<T>> {
  const settledLate: Promise<T> = pending.catch(() => fallback);
  if (budgetMs === null) {
    // 不限预算：只是把失败归成兜底值
    return settledLate.then((value) => ({ value, late: null }));
  }
  return new Promise((resolve) => {
    let decided = false;
    const timer = setTimeout(() => {
      if (decided) return;
      decided = true;
      resolve({ value: fallback, late: settledLate });
    }, budgetMs);
    settledLate.then((value) => {
      if (decided) return;
      decided = true;
      clearTimeout(timer);
      resolve({ value, late: null });
    });
  });
}
