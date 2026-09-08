# evaluation/dataset — 评测样本集

样本为 JSONL（每行一个 JSON 对象）。构造细则与难例/反例设计说明见 `docs/EVALUATION_DATASET.md`。

## 字段说明

| 字段 | 说明 |
|---|---|
| `id` | 样本 id（建议 `s001`…） |
| `type` | `normal` 普通 / `hard` 难例 / `adversarial` 反例（对抗） |
| `symbol` | `BTC-USDT` / `ETH-USDT` |
| `query` | 发往 `/api/analyze` 的问题文本 |
| `difficulty` | 1–5 主观难度（供分组统计，非答案） |
| `eval_focus` | 主压评测维度（D1…D6），可多个 |
| `rationale` | 构造意图 |
| `expected_quality_signals` | 人工预判"质量合格时应当观察到的现象"（用于事后核验 judge 是否漏判，不打分） |

> `expected_quality_signals` 只描述过程/产出特征，不预设方向结论，保证与"开放场景无标准答案"一致。

## 覆盖要求

- 普通 : 难例 : 反例 ≈ 6 : 2 : 2；
- 覆盖 BTC/ETH × {方向研判、事件归因、宏观联动、专项数据、风险边界}；
- 目标 ≥30 条；`samples.jsonl` 现含种子样本（含示例与对抗样本），请按同 schema 扩充并把规模记录进 `docs/EVALUATION_DATASET.md`。
