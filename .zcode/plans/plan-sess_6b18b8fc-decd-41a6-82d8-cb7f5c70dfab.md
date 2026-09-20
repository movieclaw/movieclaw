# 订阅投递选择性下载(只勾缺失单元对应的文件)

## 目标

整季包投递时,以暂停态交给下载器 → 读取包内文件清单 → 用现有"文件名→季集号"解析能力映射出每个文件覆盖的单元 → 对不属于本次认领缺口(含洗版单元)的视频文件取消选中(qB priority=0 / Transmission files-unwanted)→ 恢复下载。已有集数的流量不再重下,下载完成后也只有缺口文件会入库(根治"订阅 S01E08 却下整季")。

成功标准:
1. 整季包投递后,下载器中只有缺口单元对应的文件在下载,其余文件 priority=0/未选中。
2. 任何映射不确定的情况(认领单元在包内找不到文件、文件名解析不出集号等)**整体回退为全量下载**——宁可多下,绝不把认领单元的文件跳过导致工单永久挂在 GRABBED。
3. 单集资源、已存在种子(already_exists)、dry-run、电影、刷流、手动一键下载的行为与现在完全一致。

## 改动清单

### 1. `src/movieclaw_downloader`(适配器层,两个新原语)

- `base.py`:`BaseDownloader` 新增两个抽象方法:
  - `set_file_selection(info_hash, selected_indices: list[int]) -> None`——按索引重设文件选中集合(qB:`torrents_filePriority` 把未选中索引置 priority=0;Transmission:`change_torrent(files_unwanted=…)`);前置条件是刚以暂停态新添加的任务(默认全部已选中,故只需写"取消选中"那批)。
  - `resume(info_hash) -> None`——恢复暂停任务(qB `torrents_resume` / Transmission `start_torrent`)。
- `clients/qbittorrent.py`、`clients/transmission.py`:实现上述方法(实现前先对照仓库里安装的 qbittorrentapi / transmission_rpc 版本确认方法名:`torrents_filePriority` 的大小写、`change_torrent` 的 `files_unwanted` 参数)。
- `tests/downloader/unit/test_qbittorrent.py`、`test_transmission.py`:按现有假客户端注入模式补两个方法的单测(设优先级的索引换算、幂等)。

### 2. `src/movieclaw_api/services/subscription/file_selection.py`(新,纯函数规划器)

- `plan_file_selection(file_paths, needed_units, *, known_seasons=None) -> FileSelectionPlan | None`
  - 复用 `services/library/units.py` 的 `resolve_units`(批次共识借季、幻觉去噪)+ `services/library/layout.py` 的 `VIDEO_EXTS`。
  - 规则:**跳过**的只有"视频文件 且 确定解析出 (季,集) 且不在 needed 集合";非视频文件(nfo/jpg 等,体积极小)、季号挂起/集号解析失败的文件一律保留。
  - **安全阀**:任何一个 needed 单元没有任何文件保留 → 返回 None(调用方全量下载);skip 集为空 → 返回 None(无需选择,直接恢复)。
  - `FileSelectionPlan` 携带 `keep_indices / skip_indices / skip_bytes`,供日志与活动文案使用。
- 测试:新建 `tests/api/test_file_selection.py`,覆盖:`Alone.S01E08.xxx.mkv` 直取;`Season 1/Episode 8.mkv` 目录+裸集号;`Show E08` 靠批次共识;解析不出的保留;E00 特辑保留;needed 单元无文件 → None;全部需要 → None。

### 3. `services/torrent_submit.py`(编排)

- `submit_torrent` 新增可选参数 `select_units: set[tuple[int,int]] | None = None`、`known_seasons: Collection[int] | None = None`(缺省 None = 行为不变)。其余调用方(手动下载、刷流)不传,零影响。
- `select_units` 非空时:`DownloadRequest(paused=True)` 提交 → `get_torrent(include_files=True)` → `plan_file_selection` → 有 skip 则 `set_file_selection` → 恒 `resume`(含异常兜底:规划/设置过程任何异常都 log warning 并强制 resume,绝不把种子留在暂停态)。`already_exists` 或规划返回 None 时跳过优先级写入,直接 resume。
- `SubmitResult`(movieclaw_downloader/models.py)加可选字段 `skipped_file_count: int = 0`,由编排回填,供上层活动文案使用。

### 4. 订阅两个投递入口接入

- `dispatch.py`:
  - 新增小 helper:仅当 `kind=="tv"` 且 `match` 呈包形态(`pack_seasons` 非空 / `is_complete_series` / `len(match.episodes) > len(认领单元)`)时,返回 `claimed+upgrade_rows` 的单元集合,否则 None(单集包=最常见的追新路径完全不走暂停流程)。
  - `_submit_real` 透传 `select_units` + `known_seasons=subscription.selected_seasons`;GRABBED 活动:有跳过时 message 追加「选择性下载:跳过 N 个文件(仅下载缺口单元)」,payload 记 `skipped_files`。
- `replacement.py`(换源):同一处参数透传(`covered` 与 `match` 都在手,`:505-555`),让替代源整季包同样只下缺口。

### 5. 明确不做 / 不受影响(核实过)

- 不新增运行时依赖(用现有 qbittorrentapi / transmission_rpc 方法)→ **无需 bump docker/runtime-version**;无表结构变更 → 无迁移。
- 进度轮询/入库链路天然兼容:qB/Transmission 的完成度都按"已选字节"计算,选中的文件齐全则任务正常 completed;入库分批按单文件 `completed_bytes` 判定,被跳过的文件永远不完整、不会入库;`_load_delivery_provenance` 已有 `selected` 过滤先例。
- 前端不改(活动文案自然出现在订阅时间线)。

## 验证

1. `pytest tests/api/test_file_selection.py tests/downloader tests/api/test_download_submit.py tests/api/test_subscription_replacement.py -m "not integration"` 全绿;再跑相关订阅测试(检索 dispatch 既有测试文件一并跑)。
2. 全量门禁本地跑 `pytest -m "not integration"`(发版由 CI 承担,本地至少保证改动面无回归)。
3. NAS 实测(部署由你确认后另行走 nas-docker-update 流程):重新人工选种一个整季包补单集缺口,确认 qB 里未选中文件数、下载字节、订阅活动文案、入库结果(库里不新增重复版本)。

## 主要权衡(已按"简洁优先"取舍)

- **文件清单来源取下载器回读而非本地解析种子 bencode**:省一个 bencode 文件枚举器的实现,且索引与优先级写入天然同源对齐,不会错杀文件;代价是每次包投递多 2-3 次 WebUI API 往返(亚秒级)。
- **包形态才启用**(match 判定),单集资源不走暂停→恢复流程,追新热路径行为零变化。
- **不确定即全量下载**是铁律:映射失败宁可重下 10 集,也不能让认领单元的文件被跳过(那会让工单以 GRABBED 永久挂起)。