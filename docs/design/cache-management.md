# 缓存管理（Cache Management）设计与维护规范

## 1. 背景与目标

运行期数据全部落在 `data/` 目录，但在此之前它的各个子目录各自为政：图片代理
缓存有 LRU 上限，其余（播放字幕抽取、AI 字幕中间品、进度条预览图、Jellyfin
缩放变体……）都没有上限也没有入口，用户唯一能看到占用的是「更新版本目录」
一处，其余只能 SSH 上去 `du`。盘满的第一个症状往往是半夜转码写不进去。

**目标：**

1. 「设置 → 更新与维护」新增「缓存管理」标签：列出 `data/` 下每个目录的用途、
   占用与清理后果，可清理的给出清理动作，不可清理的只展示。
2. 清理是安全的：默认动作是「清理孤儿」（媒体库里已不存在的条目），「全部清空」
   对重建代价高的目录单独警示；任何清理都跳过正在使用的条目。
3. **可维护**：业务功能新增一种落盘产物时，只在登记表加一条，面板/清理/守卫
   自动覆盖；漏登记会被 CI 拦下，绕过 CI 的也会在运行时被发现。

**非目标（本期）：**

- 定时自动清扫与按目录的容量/保留期策略（下一期在登记项上加 `policy` 字段，
  由调度器低谷时统一执行；面板届时展示「上次清扫释放了多少」）；
- 低磁盘预警红点；
- 日志保留天数、图片缓存上限从环境变量迁入应用内配置。

## 2. 总体结构

```
services/storage/registry.py   登记表：data/ 下每个目录的唯一事实源
        │
        ├── services/storage/service.py   统计（快照 + 后台重算）与清理
        │        └── api/routes/storage.py  GET /app/storage、POST /app/storage/{key}/clean
        │                └── apps/web/components/app-storage-section.tsx  面板（纯视图）
        ├── lifespan.py                    启动时告警未登记条目（运行时兜底）
        └── tests/api/test_storage.py      CI 守卫：源码字面量 ⊆ 登记表、不嵌套
```

前端不写死任何目录名：分组、能否清理、说明文案全部由后端登记表给出。

## 3. 登记表与维护规范

### 3.1 一条登记长什么样

```python
DataDir(
    key="cache.trickplay",              # 稳定标识，接口/前端用
    title="进度条预览图",
    description="……重新生成需要通读整部影片抽帧……",   # 用途 + 清理后果，直接展示
    default="data/cache/playback-trickplay",         # 默认相对路径，CI 守卫按前缀匹配
    resolve=lambda s: Path(s.trickplay_cache_dir),   # 运行期真实位置（来自 Settings）
    group=Group.CACHE,                  # CACHE 可清理 / DATA 只展示
    rebuild_cost=RebuildCost.EXPENSIVE, # 面板据此警示并二次确认
    clearable=True,                     # 允许「全部清空」
    orphans=_orphans_by_id("LibraryFile"),  # 孤儿探测（批量，一次查库）
    busy=_staging_dirs,                 # 正在使用、必须跳过的条目
)
```

`orphans` / `busy` 都是「输入目录的直接子项列表，返回子集」的批量探测函数，
一次数据库查询覆盖整目录。清理只删登记目录**里面的直接子项**，从不删目录本身，
所以生产方照常 `mkdir(parents=True, exist_ok=True)`，不需要感知清理。

### 3.2 规范（硬约束）

1. **`data/` 下新增任何目录必须在 `services/storage/registry.py` 登记**，写清
   用途、能否清理、清理后果；派生物缓存至少提供一种清理方式。
2. **源码里不允许手写 `data/...` 字面量**：路径在 `core/config.py` 声明成
   Settings 字段，登记项通过 `resolve` 取值，生产方通过 `get_settings()` 取值。
3. **登记项之间不允许嵌套**：一个目录只能被统计一次；确需拆分（如
   `data/metadata` 下的 `images/` 与 `library-covers/` 策略不同）就分别登记子目录，
   父目录只是容器。

### 3.3 三道守卫

| 守卫 | 位置 | 拦什么 |
|---|---|---|
| CI 静态守卫 | `tests/api/test_storage.py::test_every_data_literal_in_source_is_registered` | 扫描 `src/` 全部 `data/...` 字面量，每一个都必须落在某条登记之下（或是登记目录的祖先容器） |
| CI 结构守卫 | 同文件 `test_registry_keys_unique_and_not_nested` / `test_registry_policy_is_consistent` | key 唯一、不嵌套；DATA 组不可清理、CACHE 组至少一种清理方式 |
| 运行时兜底 | `registry.unregistered_entries()`，启动时告警 + 面板「未登记目录」块 | 动态拼路径绕过了静态守卫的目录，管理员在面板上直接看到并可反馈 |

## 4. 统计与清理

- **统计**：`compute_usage()` 递归 `os.walk` 每个登记目录（不跟随符号链接，
  `models/ner/current` 指向的目录只算一次；SQLite 的 `-wal/-shm` 并入数据库条目）。
  大库要几十秒，所以 **`GET /app/storage` 从不阻塞**：它返回「上一次的快照 +
  `computing`」，需要重算时在线程池起一个后台任务（同时只跑一个）立即返回。
  快照没有 TTL——打开页面只看上次的结果与「统计于 N 分钟前」，什么时候重算由
  用户点刷新（`?refresh=1`）决定；进程内还没算过、以及清理动作把快照标脏之后，
  下一次读取会自动拉起后台重算。前端据 `computing` 每 2 秒轮询一次，期间页面
  继续显示旧数据，新快照落地后整体替换（清理失败/统计失败只回 `error`，
  旧数据与旧时间原样保留）。
- **清理**：`clean(key, mode)`，`mode ∈ {all, orphans}`；全局一把锁避免并发清理
  同一目录。流程：列直接子项 → `busy` 摘掉正在使用的 → 按模式选目标 → 逐项
  统计体积后删除 → 作废快照。返回删除数、跳过数与释放字节。
- **磁盘概览**：`shutil.disk_usage(data_root)`，面板分段条 = 应用数据（不可回收）
  / 可回收缓存 / 其他占用 / 剩余。

## 5. 本期登记的目录

| key | 组 | 清理 | 备注 |
|---|---|---|---|
| cache.images | 缓存 | 全部 | 已有 LRU；Jellyfin 缩放变体改走 `ImageCache.get_or_create`，纳入同一上限 |
| cache.playback_subs | 缓存 | 全部 / 孤儿 | `fonts/` 下有 staging 时整体跳过 |
| cache.subtitle_gen | 缓存 | 全部 / 孤儿 | 跳过运行中 `subtitle.generate` 任务所属文件 |
| cache.trickplay | 缓存 | 全部（警示）/ 孤儿 | 跳过 `.part` staging |
| transcodes | 缓存 | 全部 | 跳过活跃转码会话 |
| metadata.covers | 缓存 | 全部 | 下次访问重渲染 |
| metadata.images | 缓存 | 仅孤儿 | 整体重建 = 整库刷新，不给清空 |
| database / logs / uploads / updates / models / site_configs / agent.* / config / secret_key | 数据 | 无 | 只展示；更新版本数在「版本与更新」标签调整 |

## 6. 与既有机制的关系

- 图片缓存 LRU（`image_cache.py`）、日志轮转（`core/logging.py`）、转码会话的
  启动清场（`playback/session.py`）、更新目录的版本保留（`app_update.py`）都保持
  不变，面板是在它们之上的统一视图与手动入口。
- 修正：远程转码盘满的提示原来指向不存在的 `data/cache/playback`，现改为引导到
  缓存管理面板。
