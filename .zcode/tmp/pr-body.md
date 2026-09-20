两处独立修复，合入一个 PR：

## 1. 更新与维护：异常退出告警支持「知道了」确认消除

后端新增 `POST /app/update/last-exit/dismiss` 清除 entrypoint 落盘的
last-exit.json；前端告警横幅加「知道了」按钮，确认后即时隐藏，失败
提示且横幅保留。7 天自动过期保留兜底，下次异常退出仍会重新提醒。

## 2. web：发现页 Hero 首帧不再缺席推镜

首帧挂载时即带 `active=true`，推镜 `<img>` 一出生就是终态
`scale-[1.06]`，没有「1.0 → 1.06」的变化过程，transition 不起播——
表现为进 /discover/movie|tv 后首帧静止、切帧后才开始推。改为挂载完成
后再认推镜标记，首帧同样从 scale-100 缓缓推近。

## 测试

- 新增 dismiss 接口用例（`tests/api/test_app_update.py`）
- CI `pytest -m "not integration"` 门禁照常执行（本机环境缺后端依赖未跑，以 CI 为准）
