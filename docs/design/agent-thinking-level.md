# Agent 思维链强度控制（Thinking Level）

让用户自主控制模型的思考强度（思维链深度）。本文记录对 Apache Maka
（`apache/maka`）思考控制体系的调研结论与 movieclaw 的设计定案。

核心哲学一句话（Maka 定案，全盘采纳）：**统一词汇表取各家原生档位的并集，
每个模型声明自己支持的子集，菜单按子集裁剪；能直传的直传，不能直传的按
预算分段；对不上的档位不做就近映射，一律回落模型默认。**

## 1. 现状盘点

已有的地基：

| 组件 | 现状 |
|---|---|
| `ModelInfo.supports_thinking` / `max_thinking_tokens` | 已声明哪些模型会输出思考、预算上限多少，但**无消费方控制强度** |
| `compat.thinking_field` | reasoning_content 提取已工作，thinking 流式展示 + 「已思考」折叠块已上线 |
| `ModelSettings.extra_body` | 逃生舱已在用（kimi 预设注释明说 reasoning_effort 走它） |
| 补录模型表单 | 已有「支持思考」开关 + 「思考预算」数字输入（写入 max_thinking_tokens） |

缺口：统一档位词汇、按模型的档位声明、档位到各家方言的翻译、选择的存放
与回落、UI 选择器。

## 2. Maka 调研结论

### 2.1 四层架构（`core/model-thinking.ts` + `runtime/model-factory.ts`）

1. **统一词汇表**：`off|minimal|low|medium|high|xhigh|max` 七档——不是自创
   梯子，而是各家原生 effort 枚举词的**并集**；`undefined` = 「默认」（不发
   任何参数，用模型自身默认），是唯一不持久化的值。`off` 不是强度档而是
   **关闭线协议**：只有适配器存在真正的关闭编码（`reasoning_effort:'none'`、
   `thinking:{type:'disabled'}`、`thinkingBudget:0` 等，穷举为 `offBehavior`）
   才在 UI 出现，绝不发明线协议。
2. **能力声明双来源**：内置供应商由元数据目录（镜像 models.dev 的
   `reasoning_options`）推导可选档位；自定义中转的模型由**用户按模型声明**
   （`relayModelProfiles[modelId].thinkingLevels`），用户声明优先。声明词汇
   刻意排除 `off`——不假设任意中转认识关闭编码。
3. **选择的存放与回落**：全局设置只做新会话种子；主存放在**会话配置**
   （创建时带入、随时 patch，`null` 清回默认）。语义是 "a wish, not a
   guarantee"：模型菜单不含所选档位 → 当 `undefined` 回落模型默认，
   **不就近取整**；换模型清空选择——"a level is never sent to a model that
   does not understand it"。
4. **映射集中一处**（`buildProviderOptions`）：每家协议一个分支；注释原则
   "undeclared models never reach the wire"。

### 2.2 自定义模型的档位配置页（本方案 UI 的参照原型）

Maka 在 **连接详情页 → 模型管理 → 「能力声明」区** 给出了完整入口，设计
要点：

- **只对中转类连接显示**思考档位声明行——内置供应商由元数据决定，不给
  控件（"offering the control would promise an edit that cannot be saved"，
  存储编解码器会拒绝持久化，UI 不承诺存不下来的编辑）；
- **每个已启用模型一组行**：模型 id 作小标题，下挂若干 CapabilityRow——
  思考档位（多选下拉）、视觉输入（三态：自动/启用/禁用）、上下文窗口
  （数字覆盖）；一行一个声明，"label + 说明在左、一个紧凑控件在右"；
- **思考档位是多选下拉**（checkbox items）：候选 = 可声明词汇（排除 off）
  ∪ 已存表里额外出现的值（手改配置文件写进的值保持可见可取消，绝不变成
  隐形选择）；档位按 low→max 固定顺序，触发器显示「已选择 N 个」/「未声明」；
- **多模型批量声明行**：一个中转通常前面是同一家族、接受相同 effort 值的
  模型，逐行声明是 模型数×档位数 次点击——表格顶部一个批量下拉，checkbox
  带覆盖度计数（「3/5 个模型」），全覆盖才打勾；
- **草稿与保存**：草稿按连接隔离（切换连接即重置），seed 经过与运行期
  同一个 sanitizer——草稿显示的就是保存后会生效的；「保存能力声明」落到
  连接文档，与启用模型列表联动（禁用模型删除其声明）。

### 2.3 会话内 UI

composer 的模型切换器旁一个「思考级别」选择器，文案
`默认/关/最少/低/中/高/超高/最高`；不支持的模型隐藏选择器并提示。

## 3. movieclaw 与 Maka 的关键差异

Maka 各协议均为 effort 枚举制；movieclaw 的四家 OpenAI 兼容端点**方言
分裂**，这是本方案唯一需要自创的部分：

| 端点 | 控制方式 | 归类 |
|---|---|---|
| Kimi 官方（k3） | 顶层 `reasoning_effort: low/high/max` | **effort 枚举制**（思考不可关） |
| 百炼 qwen 系 | `enable_thinking` bool + `thinking_budget` int | **budget 预算制** |
| GLM 4.5+ | `thinking: {type: enabled/disabled}` | **toggle 开关制** |
| DeepSeek 官方 | 无强度参数（实现时以最新文档为准） | 不可控（不声明） |

## 4. 设计定案

### 4.1 词汇表（照抄 Maka 七档）

```python
ThinkingLevel = Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"]
```

UI 文案：关/最少/低/中/高/超高/最高；`None` = 「默认」。词汇表是并集不是
私有梯子，供应商发明新词时扩表收编，未收编前静默丢弃（Maka 同款注释）。

### 4.2 控制方言声明（`ModelInfo.thinking_control`）

```yaml
# preset yaml 模型级新增字段；用户补录模型（extra_models）同结构
thinking_control:
  kind: effort            # effort | budget | toggle
  levels: [low, high, max] # effort 制必填：该模型的原生档位子集 = 菜单
  supports_off: false     # 是否有真关闭协议（qwen/glm true，kimi-k3 false）
```

三种 kind 的翻译规则（实现集中在 `openai_chat` 协议层一处）：

- **effort**：档位原词直传（SDK 已知参数走顶层，其余并入 `extra_body`）；
  菜单 = `levels`（+ supports_off 时的「关」）；
- **budget**：档位 → `enable_thinking=true` + `thinking_budget` 按
  `max_thinking_tokens` 比例分段——**低 25% / 中 50% / 高 100%**（向下
  取整）；菜单固定 关/低/中/高（supports_off=false 时无关）；
  `max_thinking_tokens` 未声明的模型退化为 toggle（只有开关，没有预算可分）；
- **toggle**：菜单 = 默认/关；关 → 该方言的关闭编码（如
  `thinking:{type:"disabled"}`）。

**fail-closed**：未声明 `thinking_control` 的模型无菜单、档位永不落请求。
`supports_thinking` 回归单一职责（是否会输出 reasoning_content），不再
兼职暗示可控性。用户 `extra_body` 里显式写的同名参数**优先于**翻译结果
（逃生舱语义不变）。

### 4.3 选择的存放与回落

- `ModelSettings.thinking_level: ThinkingLevel | None = None`（None=默认）；
- `SessionStartPayload` / `SessionRetryPayload` 增加可选 `thinking_level`；
  **对外协议只收词汇表枚举**，翻译由服务端完成（与图片方案的
  「ContentPart 永不外部直传」同一安全口径）；
- **会话记忆零迁移**：user 消息的转录信封（`SessionMessageEntry`）存
  `thinking_level`，续聊未显式传参时沿用最近一条 user 行的值——与
  `compact-context` 「沿用会话最近一次使用的模型」同一先例，不给
  agent_session 表加列；
- 回落语义（Maka 同款）：resolve 失败（当前模型菜单不含该档）→ 不发参数
  + info 日志，绝不就近取整；前端换模型时清空选择，服务端 resolve 兜底；
- 内部调用（上下文压缩摘要、未来的自动命名等）一律不带档位（默认），
  不放大成本。

### 4.4 UI

**会话 composer**：加号旁一个「思考」pill 下拉（当前会话模型的菜单渲染；
无菜单模型隐藏整个控件）。选中值随消息提交，写入转录信封。

**补录模型表单 = 完整的「能力声明」表**（设置 → AI 模型）：

自定义接入的声明不止思考——多模态（图片/视频输入）与思考控制属于同一
类事实（「这个模型能吃什么、怎么控」），在表单上收拢成一节。Maka 的
`RelayModelProfile`（thinkingLevels + vision + contextWindow）就是同一个
思路的按模型声明对象；movieclaw 的对应物是 `ModelInfo` 本身，补录表单
即它的编辑器：

```
新模型参数
  ├─ 模型 ID * / 上下文窗口 * / 最大输入 / 最大输出 *      ← 现状
  ├─ 工具调用 [✓]   并行工具调用 [ ]                       ← 现状
  ├─ 多模态
  │    ├─ 图片输入 [ ]   → modalities 含 image（现有「视觉」勾选归位到
  │    │                   这里；图片门控 fail-closed 的放行出路，已实现）
  │    └─ 视频输入 [ ]   → modalities 含 video（声明先行：预设目录已在
  │                        携带 video，Agent 侧视频输入是后续能力，当前
  │                        门控只读 image；表单 hint 注明「暂未支持发送
  │                        视频，声明用于能力就绪」）
  ├─ 支持思考  [✓]        → 是否输出思考内容（reasoning_content 展示）
  │    ├─ 思考控制   ( ) 不可控（只展示思考内容）      ← 默认，即不声明
  │    │            ( ) 档位直传（reasoning_effort）
  │    │            ( ) 预算分段（enable_thinking + thinking_budget）
  │    │            ( ) 仅开关（thinking.type）
  │    ├─ [档位直传时] 可选档位：多选 chips（最少/低/中/高/超高/最高，
  │    │              固定顺序，触发器显示「已选 N 档」/「未声明」） ← Maka 同款
  │    ├─ [预算分段时] 思考预算 *（沿用现有 max_thinking_tokens 输入，
  │    │              作为「高」档基准，低/中按 25%/50% 折算）
  │    └─ 支持关闭  [ ]（有真关闭协议才勾；档位直传制勾选后菜单多出「关」）
```

模型目录列表的能力徽标同步补齐：现有「视觉」徽标旁增加「视频」与
「思考可控」，用户扫一眼就知道每个模型声明了什么。

三点取舍：

- **视觉声明用两态勾选，不抄 Maka 的三态（自动/启用/禁用）**：Maka 需要
  显式 disabled 是因为运行时会相信供应商 /models 上报的视觉能力，用户要
  有推翻它的手段；movieclaw 没有自动探测层，目录/补录是唯一事实源，
  缺席即纯文本（fail-closed），两态足够；
- **内置预设模型不给声明控件，但「同 id 补录覆盖」是修目录的正道**：
  movieclaw 的合并规则本就是用户条目按 id 覆盖预设（`_catalog`），预设
  过时（如 kimi-k3 实际已支持视觉而目录标纯文本）时，用户同 id 补录一条
  完整声明即可修正——这比 Maka 更进一步：Maka 的内置目录用户改不了；
- **Maka 的批量声明行不做**——movieclaw 的补录是逐模型表单而非多模型
  能力表格，没有 模型数×档位数 的点击爆炸问题；将来若改表格式再补。
  照抄的原则不变：已存声明里出现的词汇表外值保持可见可取消。

### 4.5 预设目录首批声明

- kimi：`kimi-k3 → effort [low, high, max]`（k2.6/k2.5 按百炼口径核实后补）；
- bailian：qwen3.7/3.6 系 → `budget, supports_off: true`（预算基准 =
  已声明的 max_thinking_tokens）；deepseek-v4 系不声明（无公开控制参数）；
- glm：4.5+ → `toggle, supports_off: true`；
- deepseek / openai_compat 通用预设：不声明，用户按模型补录。

## 5. 落地步骤（每步可独立验证）

```
1. movieclaw_llm：ThinkingLevel 词汇 + ModelInfo.thinking_control +
   ModelSettings.thinking_level + openai_chat 三种方言翻译（含 extra_body
   合并优先级）
   → 验证：单测覆盖 effort 直传 / budget 分段取整 / toggle 关闭 /
     未声明不发参 / 用户 extra_body 覆盖翻译结果 / off 仅 supports_off 可用
2. 预设目录声明（kimi/bailian/glm）+ resolve 回落
   → 验证：档位不在菜单 → 请求体无思考参数 + 日志；表格数值抽查
3. API：SessionStartPayload/retry 传参 + 转录信封持久化 + 续聊沿用
   → 验证：API 测试覆盖 显式传参、缺省沿用上一条、换模型后失效回落默认
4. Web：composer 思考 pill（按模型菜单渲染/隐藏）+ 补录模型表单扩展
   → 验证：组件测试 + 手工（kimi-k3 三档、qwen 四档、glm 两档、
     deepseek 隐藏）
```

全链路验收：kimi-k3 选「低」与「最高」各问同一道推理题，观察思考段长度
与耗时差异明显；qwen 选「关」后响应无 reasoning_content；档位在会话内
换模型到 deepseek 后 UI 显示回「默认」、请求体无思考参数。

## 6. 已知取舍

- **不做档位就近映射**：成本不可预期（中悄悄变 max）、语义不可解释；
  回落默认 + UI 如实显示，Maka 明确拒绝就近取整，我们照抄；
- **budget 分段比例（25/50/100）是自创约定**：Maka 无预算制先例；比例
  写成常量，等真实使用反馈再调；
- **不同模型菜单档位数不一**（kimi 三档、qwen 四档、glm 两档）：是诚实
  不是缺陷，接受；
- **会话记忆存转录信封而非 DB 列**：零迁移，代价是「会话当前档位」要扫
  最近 user 行——与既有 model 沿用逻辑同路径，成本可忽略。
