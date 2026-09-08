# CryptoSage — 基于 Hy3 的加密货币研判多 Agent 系统（含自定义评估体系）

> **2026 犀牛鸟开源人才培养计划 · 实战任务《混元大语言模型项目》**
>
> 选题：**任务一 · 开放式场景：AI 应用与评判标准设计**
> （`Hy3 application with custom evaluation criteria for open-ended tasks`）
>
> **⚠️ 本仓库为参赛者个人创作与维护的个人 / 活动作品，非腾讯官方发布。**

---

**一句话**：一个基于 Hy3「多档思考 + Function Calling」的**多 Agent 协作应用**——对 BTC/ETH 融合技术面、链上、衍生品、舆情、宏观五类信号，产出**带置信度的方向研判、关键价位、风险边界与规则化行动建议**；同时，本仓库按实战任务要求，为这种"无唯一标准答案"的开放场景**自定义了一套评估方法**，并给出评测材料、评测脚本与有效性验证（见 [评测体系](#-评测体系开放式场景自定义评估) 与 [`docs/`](docs/README.md)）。

> ⚠️ **免责声明**：本工具仅供研究学习与教育目的使用，所有分析结果均不构成任何形式的投资建议。加密货币市场具有极高风险，请自行判断并承担投资风险。应用侧的输出由 Hy3 通过 API 调用完成，全程无训练 / 微调 / 本地推理。

---

## 目录

- [这个项目解决什么问题](#这个项目解决什么问题)
- [仓库组成（应用 + 评测 = 一个作品）](#仓库组成应用--评测--一个作品)
- [系统架构](#系统架构)
- [Hy3 在系统中承担的角色](#hy3-在系统中承担的角色)
- [快速开始](#快速开始)
- [API 一览](#api-一览)
- [评测体系（开放式场景自定义评估）](#评测体系开放式场景自定义评估)
- [目录结构](#目录结构)
- [环境变量](#环境变量)
- [合规与安全](#合规与安全)
- [Demo](#demo)
- [参与方式说明](#参与方式说明)
- [License](#license)

---

## 这个项目解决什么问题

**目标用户**：需要快速获得"多维度、可交叉验证、可追溯"的加密市场研判的投资者 / 研究人员。他们面对的是单一信息源偏差、确认偏误、以及"看似有理却无法归因"的分析结论。

**待解决的问题**：把"分析师如何看市场"的过程显式化——由不同维度（技术面 / 链上 / 衍生品 / 舆情 / 宏观）独立取证，交叉验证后融合，并由 Critic 对抗审查，最终给出**带置信度的概率化研判与风险边界**，而不是拍脑袋式的精确点位预测。

**为什么必须引入大模型**：
1. 该场景是多源异构信息的实时决策问题，需要 Agent 自主决定"查什么、何时停止、怎么综合"；
2. 结论的"可解释推理链"与"风险边界"本身是产出的一部分，需要生成式能力把证据组织成可读的研判报告；
3. 判断（该看哪些证据、证据是否冲突）是开放任务，没有固定程序可写，必须依赖 Hy3 的推理与工具调用能力。

---

## 仓库组成（应用 + 评测 = 一个作品）

本实战任务要求"AI 应用 + 自定义评判标准 + 评估验证"作为一个整体交付，因此本仓库除了可运行的应用外，还包含完整的评测材料：

| 实战任务产出要求 | 本仓库位置 |
|---|---|
| 开源项目仓库：应用源码、README、环境配置样例与运行说明 | 仓库根目录（本文档 + `.env.example` + `backend/` + `frontend/`） |
| 评测样本集 | [`evaluation/dataset/`](evaluation/dataset/README.md)（含构造说明与难例 / 反例设计） |
| 评估方法说明文档 | [`docs/EVALUATION_METHOD.md`](docs/EVALUATION_METHOD.md) |
| 评测脚本 | [`evaluation/scripts/`](evaluation/scripts/README.md) |
| 完整结果表格 | [`docs/EVALUATION_RESULTS.md`](docs/EVALUATION_RESULTS.md) |
| 有效性验证结果（判别力 / 一致性） | [`docs/VALIDATION.md`](docs/VALIDATION.md) |
| 分析报告（场景理由 / 评估维度依据 / 失败模式 / 能力边界） | [`docs/ANALYSIS_REPORT.md`](docs/ANALYSIS_REPORT.md) |
| ≤2 分钟 demo 视频 / GIF | [`docs/demos/`](docs/demos/README.md) |

---

## 系统架构

```
┌────────────────────────────────────────────────────────────────┐
│  前端 (React + TypeScript + Vite + Ant Design + ECharts)         │
│  · 分析输入 · Agent 决策流/思考过程实时可视化（WebSocket）        │
│  · K线 / 信号雷达 / 事件图谱 / 校准监控 / 组合统计                 │
└────────────────────────────┬───────────────────────────────────┘
                             │ REST `/api/*` + WS `/ws/{task_id}`
┌────────────────────────────▼───────────────────────────────────┐
│  后端 (FastAPI)                                                  │
│  · /api/analyze 提交任务 → 事件缓冲 + WS 增量推送                 │
│  · /api/report/{id} 报告回放 · /api/backtest 影子回测             │
│  · /api/calibration/agents 动态可靠度 · /api/monitor/drift 漂移    │
└────────────────────────────┬───────────────────────────────────┘
┌────────────────────────────▼───────────────────────────────────┐
│  Agent 编排层 (LangGraph StateGraph, 两层 Loop)                   │
│  Loop1: Orchestrator → 并行调用专家 → 证据池评估 → 继续/进入融合    │
│  Loop2: Synthesis ↔ Critic（有效质疑则定向修正，直至收敛）          │
│  专家 Agent：技术面 / 链上 / 衍生品 / 舆情 / 宏观                   │
└────────────────────────────┬───────────────────────────────────┘
┌────────────────────────────▼───────────────────────────────────┐
│  数据与判定层                                                     │
│  · Function Calling 工具：K线/指标/关键价位、鲸鱼净流、资金费率/OI/ │
│     爆仓/期权、恐惧贪婪、宏观新闻检索                             │
│  · 确定性校准：方向/置信度由证据确定性计算（覆盖度×一致度×强度）， │
│     数据质量 real/partial/degraded 参与加权，新闻时效滞后封顶置信度 │
│  · 影子回测：每次方向性研判进入影子追踪，24h 固定窗口自动结算，     │
│     产出胜率/Brier 分并反哺 Agent 动态权重与漂移告警               │
│  · SQLite 本地缓存（行情/历史分析/回测记录）                       │
└────────────────────────────────────────────────────────────────┘
```

**两条 Loop 的设计**：

1. **Loop 1 — Orchestrator ReAct 主循环**：理解用户问题 → 决定并行调用哪些专家 Agent → 汇总证据后评估"数据是否足够" → 不足则再调 Agent / 足够则进入融合（有早退与去重机制防空转）。
2. **Loop 2 — Synthesis ↔ Critic 对抗循环**：Synthesis 融合五维信号输出初步研判；Critic 审查推理漏洞与过度自信，有效质疑将触发 Synthesis 定向修正，直至 Critic 无有效质疑或达到轮次上限。

**判定与鲁棒性要点（也是评估所关注的"过程质量"对象）**：

- **方向 / 置信度不靠 LLM 自评**：由证据确定性公式计算（数据覆盖、源可信度、方向一致度、信号强度），LLM 负责市场状态识别、解释与风险描述；Critic 折扣最多生效一次（≤10 个百分点）。
- **数据质量分级**：每个 Agent 信号带 `data_quality: real / partial / degraded`，合成时按质量折权，少源场景置信度封顶（单一方向源 ≤54%，数据不足 ≤35%），不会"假装共识"。
- **新闻时效守卫**：宏观新闻在最终研判完成时若已滞后 >5 分钟，方向置信度自动封顶 45%。
- **可审计事件图谱**：宏观事件按独立来源数与来源质量确认；政府抛售类事件必须"链上入金 / 官方拍卖公告 / 双独立来源"三者取证后才会升级状态，否则仅标注"未确认"。
- **反事实压力测试**：对每个方向性信号做 neutralize / flip 反事实推演，展示结论对单一证据的敏感度。

---

## Hy3 在系统中承担的角色

| 能力 | Hy3 特性 | 在本系统的用途 |
|---|---|---|
| Agent 规划与调度 | Agent / 任务规划 | Orchestrator 决定调用哪些专家 Agent、是否继续循环 |
| Function Calling | 标准 `tools` + `tool_choice=auto` | 数据 Agent 自主抓取 K线 / 链上 / 衍生品 / 舆情 / 宏观数据 |
| 多档思考 | `reasoning_effort: high / low` | 研判类 Agent 用慢思考保证推理质量，数据类 Agent 用快思考控制成本 |
| 交错式思考 | 慢思考下"思考 + 工具调用"交错 | Synthesis / Critic 的连续推理与工具联动 |
| 长上下文 | Hy3 原生超长上下文 | 一次性承载多 Agent 证据与历史上下文做综合研判 |
| 结构化输出 | JSON 结构化约束 | 各 Agent / 融合层输出结构化信号与报告，便于确定性融合与前端渲染 |

> 模型通过 OpenAI 兼容接口调用（`HY3_BASE_URL`，默认 `https://tokenhub.tencentmaas.com/v1`）；客户端支持 Hy3 主用、DeepSeek 备用的自动 failover（`LLM_PROVIDER=hy3|deepseek`）。

---

## 快速开始

### 环境要求

- Python 3.10+（后端）
- Node.js 18+（前端，仅开发 / demo 需要）
- Hy3 API Key（`LLM_PROVIDER=hy3` 时必填）

### 1. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 至少填入：
#   LLM_PROVIDER=hy3
#   HY3_API_KEY=你的 Hy3 API Key
# 可选：
#   ETHERSCAN_API_KEY=...   （链上数据）
```

### 2. 安装后端依赖

```bash
pip install -r requirements.txt
# 或 pip install -e .
```

### 3. 启动后端

```bash
python -m backend.main
# 等价于：uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
# 健康检查：http://localhost:8000/health
```

### 4.（可选）启动前端开发环境

```bash
cd frontend
npm install
npm run dev
# 前端运行在 http://localhost:3000，/api 与 /ws 已代理到后端 8000
```

### 5. 一键启动（含 .env / 依赖 / 前后端进程管理）

```bash
./start.sh           # 前台
./start.sh start     # 后台；stop / restart / status 见脚本头注释
```

### 6. Docker（生产 / 展示模式，单端口）

```bash
docker build -t cryptosage .
docker run -p 8000:8000 --env-file .env cryptosage
# 后端自动托管构建后的前端静态资源，访问 http://localhost:8000
```

### 7. 使用

1. 打开 `http://localhost:3000`（或 Docker 模式 `http://localhost:8000`）
2. 选择 `BTC-USDT` / `ETH-USDT`，输入问题（如"分析 BTC 当前是否适合进场"、"ETH 刚才突然大跌，发生了什么？"）
3. 实时查看 Orchestrator 规划、专家并行取证、Synthesis 融合、Critic 对抗，以及带置信度的最终研判报告

---

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/analyze` | 发起分析，返回 `task_id`（并发上限默认 4，超出返回 429） |
| DELETE | `/api/analyze/{task_id}` | 取消分析 |
| GET | `/api/klines` | 行情 K线代理（OKX） |
| GET | `/api/report/{task_id}` | 获取最终报告（含回放用的执行轨迹与执行指标） |
| GET | `/api/history` | 历史分析记录 |
| GET | `/api/backtest` | 影子回测记录与统计 |
| GET | `/api/calibration/agents` | Agent 动态可靠度（准确率 / Brier / 权重乘子） |
| GET | `/api/monitor/drift` | Agent 近期 vs 基线漂移告警 |
| GET | `/api/shadow/stats` | 影子组合统计、漂移告警、动态权重 |
| POST | `/api/backtest/resolve`、`/api/shadow/resolve` | 手动结算到期影子记录 |
| WS | `/ws/{task_id}` | 事件流推送（缓冲 + 游标补发，断线重连） |
| GET | `/health`、`/ready` | 健康 / 就绪探针 |

---

## 评测体系（开放式场景自定义评估）

本场景下"一次分析结论是否优秀"没有唯一标准答案，因此按实战任务要求自定义了一套评估方法，核心设计为 **5 个评估维度 + 可操作判定标准 + LLM-as-judge 与规则校验结合的自动评测 + 判别力 / 一致性验证**：

| 维度 | 一句话定义 |
|---|---|
| D1 事实与数据准确性 | 报告中的行情数值、指标值、事件主张是否与输入证据一致，是否存在编造 |
| D2 证据可追溯性 | 每条关键结论是否可回指到具体 Agent / 数据源 / 工具输出 |
| D3 推理与融合一致性 | 多源是否存在矛盾却被忽略、权重是否与市场状态匹配、Critic 是否有效 |
| D4 风险与合规表达 | 风险边界、失效条件、免责与尾部风险是否被充分、合规地表达 |
| D5 结构化与可读性 | 输出字段是否完整符合 schema、格式是否规范、语言是否易懂 |

每个维度都有 1–5 分的行为锚定（anchored rubric），避免"较好地满足 / 基本符合"这类不可判定的表述。

评测体系与材料在：

- **设计文档**：`docs/EVALUATION_METHOD.md`（维度、判定标准、评分方式与设计依据）
- **评测样本集**：`evaluation/dataset/`（含难例 / 反例构造说明与覆盖矩阵）
- **评测脚本**：`evaluation/scripts/`（自动执行一轮评测并输出结果）
- **结果表格与归因**：`docs/EVALUATION_RESULTS.md`
- **有效性验证**：`docs/VALIDATION.md`
- **分析报告**：`docs/ANALYSIS_REPORT.md`

> 评测执行需要 Hy3 API Key 与本地数据源可达性；结果表格中当前尚未回填的部分为"待评测运行后按脚本产出回填"，不属于断言性结论。

---

## 目录结构

```
.
├── README.md                    # 本文件（项目 + 评测体系总览）
├── LICENSE                      # MIT License（个人作品）
├── .env.example                 # 环境变量样例（密钥不入库）
├── requirements.txt / pyproject.toml
├── start.sh                     # 一键启停脚本（后台模式）
├── Dockerfile
├── docs/                        # 实战任务文档
│   ├── README.md                # 文档索引
│   ├── EVALUATION_METHOD.md     # 评估方法设计说明（维度与判定标准）
│   ├── EVALUATION_DATASET.md    # 评测样本集说明（来源/构造/难例反例）
│   ├── EVALUATION_RESULTS.md    # 完整评测结果表格与 case 归因
│   ├── VALIDATION.md            # 有效性验证（判别力 / 一致性）
│   ├── ANALYSIS_REPORT.md       # 分析报告（场景/方案/失败模式/能力边界）
│   └── demos/                   # ≤2min demo 视频/GIF 说明与放置位置
├── evaluation/                  # 评测材料
│   ├── README.md
│   ├── rubric.md                # 分维度评分细则（anchored rubric）
│   ├── dataset/                 # 评测样本集（JSONL + 说明）
│   └── scripts/                 # 评测脚本（运行一轮评测 → 结果）
├── backend/
│   ├── main.py                  # FastAPI 入口（REST + WS + 静态托管）
│   ├── config.py                # 全量环境变量配置
│   ├── llm/hy3_client.py        # Hy3/DeepSeek 统一客户端（failover + FC 循环）
│   ├── agents/                  # LangGraph 编排与各 Agent
│   ├── calibration/performance.py  # 影子回测驱动的 Agent 可靠度校准
│   ├── data/                    # 数据客户端 + SQLite + 新闻事件图谱
│   ├── indicators/              # 技术指标与关键价位算法
│   ├── tools/definitions.py     # Function Calling 工具 schema
│   └── utils/
├── frontend/                    # React + TS + Vite
└── tests/                       # 单元 / 冒烟测试（pytest）
```

---

## 环境变量

密钥全部通过环境变量 / `.env` 传入，**仓库内不包含任何真实密钥**。完整清单见 [`.env.example`](.env.example)，常用项：

| 变量 | 必填 | 说明 |
|---|---|---|
| `LLM_PROVIDER` | 是 | `hy3`（默认）或 `deepseek` |
| `HY3_API_KEY` | 是* | Hy3 API Key（`LLM_PROVIDER=hy3` 时） |
| `HY3_BASE_URL` | 否 | 默认 `https://tokenhub.tencentmaas.com/v1` |
| `HY3_FAST_MODEL` / `HY3_SLOW_MODEL` | 否 | 快 / 慢思考模型名 |
| `ETHERSCAN_API_KEY` | 否 | 链上数据（免费档） |
| `ENABLE_POSITION_ADVICE` | 否 | `false` 时只输出信号与风险、不输出仓位建议 |
| `MAX_ORCHESTRATOR_STEPS` / `MAX_CRITIC_ROUNDS` | 否 | 两层 Loop 的轮次上限（防死循环兜底） |
| `BACKTEST_HORIZON_HOURS` / `BACKTEST_ROUND_TRIP_COST_BPS` | 否 | 影子回测结算周期与交易成本 |
| `MACRO_SEARCH_PROVIDER` | 否 | 宏观联网搜索 provider |
| `SSL_VERIFY` | 否 | 企业网络特殊场景下可关闭（生产保持 `true`） |

---

## 合规与安全

- **模型能力调用全程通过 Hy3 API**，无训练 / 微调 / 本地推理部署；
- **密钥管理**：全部 env-only（`.env` 已在 `.gitignore`），代码无硬编码；
- **SSRF 防护**：外部请求仅限配置内的白名单域名（OKX / Binance / Etherscan / Alternative.me / Gate / Deribit / mempool.space / 新闻源等）；
- **投资合规**：应用内与 README 明确"仅供研究学习，不构成投资建议"，默认对低置信度结果降级为"观望"且不产生真实仓位；
- **对外署明**：行情与数据均来自公开 API，README 与相关文档标注数据来源（Etherscan APIs 等）。

---

## Demo

见 [`docs/demos/README.md`](docs/demos/README.md)。至少覆盖两个端到端流程（建议 ≤2 分钟 GIF / 视频）：

1. **完整多 Agent 研判**："分析 BTC 当前是否适合进场"——展示编排 → 专家并行 → 融合 → 对抗 → 最终报告；
2. **事件驱动快速分析**："ETH 刚才突然大跌，发生了什么？"——展示衍生品 / 链上 / 宏观联动与新闻时效守卫。

---

## 参与方式说明

本作品由参赛者本人定义需求与验收标准，并借助 AI 编程助手 **CodeBuddy** 辅助完成代码实现（vibe-coded），人工负责审阅、测试与调优，遵守"人工审阅兜底、AI 辅助实现"的开发规范。本仓库为**个人参赛作品**，与腾讯官方发布无关。

---

## License

[MIT](LICENSE) © 2026 Liao330（个人参赛作品）。

> 本项目为基于腾讯开源模型 Hy3（Apache-2.0）API 构建的应用层作品，未包含、复制或修改 Hy3 模型本体代码。
