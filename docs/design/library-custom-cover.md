# 媒体库自定义封面

> 背景：issue #427「增加自定义媒体库封面功能」——用户想用自己修改好的封面，
> 而不是只能吃系统自动生成的那张。

## 1. 问题

今天库封面只有一种来源：`services/library/cover.py` 服务端渲染的「氛围光货架」
拼贴（库内最近入库的 4 部作品海报）。它够体面，但**不可干预**——用户做好了一张
海报也放不进去。

约束有两条：

- 封面有**三个消费方**（控制台首页卡片、管理页缩略图、Jellyfin 库 Primary 图），
  不能只改其中一个；
- 用户丢过来的是**手机原图 / 4K 截图**，动辄 5~10MB。原样落盘等于让每张库卡片
  背一个几 MB 的请求，NAS 上传给手机端更难看。

## 2. 设计结论

### 2.1 一个短路点，三端全通

三个消费方早就收口在 `ensure_library_cover(library_id) -> (路径, 版本 key)` 这一个
入口上。自定义封面在它**最前面**短路：

```
ensure_library_cover(id)
  ├─ 有自定义封面？→ 直接返回 (uploads/library-covers/{id}.jpg, key)   ← 一次 stat
  └─ 没有 → 原拼贴链路（选素材 → 指纹 → 渲染 → 缓存）
```

下游一行都不用改，Jellyfin 的 `ImageTags.Primary`、控制台的 ETag 全部自动跟上。
版本 key 与拼贴**同形**（32 位 hex，取 `md5(路径+mtime_ns)`）——它会被当成
Jellyfin 的 image tag 发给第三方播放器，不值得赌各家实现对异形 tag 的宽容度。

### 2.2 压缩规格（服务端为准）

前端选图后先用 `fileToCompressedJpeg(file, 1600)` 压一道，省上传体积；但**保证在
服务端**——CLI 与第三方客户端绕得过浏览器。`normalize_cover_image()` 的流水线：

| 步骤 | 做法 | 为什么 |
|---|---|---|
| 收前限流 | ≤ 10MB（与首页背景图一致） | 防滥用硬闸 |
| 真解码 | Pillow `Image.open`，**不信** `Content-Type` | MIME 是上传方说了算的；顺带把可内嵌脚本的 SVG 挡在门外（Pillow 不认它） |
| 像素上限 | > 5000 万像素直接拒 | 解压炸弹：10MB 的 PNG 能解出几个 GB 位图。看 `size` 不碰像素，零成本 |
| 摆正 | `ImageOps.exif_transpose` | 手机横拍的图否则是躺着的 |
| 缩放 | 长边 > 1600 → LANCZOS 等比缩到 1600，**只缩不放** | 卡片 2 倍屏下也就 800 逻辑像素宽，拼贴本体才 1260 宽 |
| 扁平化 | RGBA/P 合成到 `#080a10` 底 | JPEG 没有 alpha；这个底色与货架背景一致 |
| 编码 | JPEG q85 + `optimize` + `progressive` | EXIF（含 GPS）随重编码一起丢掉 |

实测（高质量 JPEG 输入）：

| 输入 | 产物 |
|---|---|
| 手机原图 4032×3024 / 5.4MB | 1600×1200 / **115KB**（降 98%，约 260ms） |
| 4K 截图 3840×2160 / 3.6MB | 1600×900 / **94KB**（降 97%，约 180ms） |
| 1280×720 / 408KB | 原尺寸 / 146KB（降 64%） |

两个刻意的取舍：

- **JPEG 而不是 WebP**。WebP 同画质能再小约两成，但 Jellyfin 图片路由本就按
  `image/jpeg` 输出，第三方播放器（VidHub / Infuse / Emby 系）对 WebP 支持参差。
  省下的两成换不来「封面不显示」这类难排查的报障。
- **不裁剪**。各消费方本来就 `object-cover` 按自己的卡片比例取景，用户精心做的图
  不该被我们先切一刀。上传处给一句「推荐 21:10（如 1260×600）」的提示即可。

解码与重编码是 CPU 活（大图几百毫秒），走 `asyncio.to_thread`，与拼贴渲染同规矩。

### 2.3 存储：uploads 而不是 metadata

落 `data/uploads/library-covers/{库id}.jpg`，一库一槽位。

**不能**放 `data/metadata/library-covers/`——那是存储登记里的 `CACHE` 组、
`clearable=True`（拼贴随时能重渲，清了无所谓）；用户自己做的图清掉就没了。
`uploads` 是不可清理的用户数据组，语义才对。

登记项不得嵌套（`tests/api/test_storage.py::test_registry_keys_unique_and_not_nested`），
所以不新增条目，只把 `uploads` 那条的文案补上封面。

**不加数据库列、不做迁移**：「有没有自定义封面」= 文件在不在（与首页背景图同一套
思路）。代价是删库时要手动收尾——`LibraryConfigService.delete` 里删掉封面文件，
否则就是个永久孤儿，而它所在的组还不提供清理。

## 3. 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/libraries/{id}/cover` | multipart 上传，`require_admin`（与 `library.update` 同权限）。返回 `{version, bytes}`，version 给前端打缓存 |
| `DELETE` | `/libraries/{id}/cover` | 删掉自定义图，封面回落自动拼贴 |
| `GET` | `/libraries/{id}/cover` | **不变**，只是可能吐自定义图 |

`LibraryView` 加一个 `custom_cover: bool`：前端据此决定按钮形态，以及**空库**要不要
照样出图（首页卡片原本的条件是「有海报素材才出图」）。

## 4. 前端

编辑库弹窗（`library-form-dialog.tsx`）新增「封面」分区：当前封面预览 +
「上传封面 / 换一张」+「恢复自动拼贴」。

两点与这个弹窗里其它字段不同，文案里说明白了：

- **上传即生效，不等「保存」**——它是一次文件传输而不是表单字段，攒到保存再传只会
  让失败反馈来得更晚；因此弹窗**取消关闭时也要刷新**列表。
- 换图后 `<img>` 不会因为 ETag 变了自己重取（那是 DOM 行为，不是缓存行为），所以
  `libraryCoverUrl(id)` 里带一个上传/删除时更新的时间戳。这个 helper 顺手收掉了原先
  三处各自拼 URL 的重复。

建库向导里不放——那时还没有库 id。

## 5. 明确不做

- 每个**条目**（影片/剧集）的自定义封面：那是刮削选图的地盘（`artwork.py` 已有
  「选图」链路），不在本期；
- 保留上传原图：只存归一化后的那份，想换重传即可；
- 按 `Accept` 同时供 WebP 与 JPEG 两份：为这点体积把链路复杂化不划算。
