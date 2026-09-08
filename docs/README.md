# docs 文档索引

本目录存放【犀牛鸟开源-实战任务《混元大语言模型项目》· 任务一（开放式场景：AI 应用与评判标准设计）】的完整交付文档。

> 本文档为个人参赛作品配套材料，非腾讯官方发布。

## 文档清单

| 文档 | 对应实战任务要求 | 内容 |
|---|---|---|
| [EVALUATION_METHOD.md](EVALUATION_METHOD.md) | 评估方法设计 | 场景定义、5 个评估维度、可操作判定标准（anchored rubric）、评分方式与设计依据 |
| [EVALUATION_DATASET.md](EVALUATION_DATASET.md) | 评测样本集 | 样本来源、构造方式、覆盖范围、难例 / 反例设计与比例 |
| [EVALUATION_RESULTS.md](EVALUATION_RESULTS.md) | 评测执行 / 结果表格 | 一轮完整评测的结果表格、典型 case 归因（待按脚本回填） |
| [VALIDATION.md](VALIDATION.md) | 有效性验证 | 判别力验证与一致性验证的实验设计与记录 |
| [ANALYSIS_REPORT.md](ANALYSIS_REPORT.md) | 分析报告 | 场景选择理由、AI 方案、评估维度设计依据、评测结论、模型失败模式与能力边界、典型模式 |
| [demos/README.md](demos/README.md) | ≤2min demo | demo 录制说明与放置位置 |

## 评测材料对照

| 实战任务产出 | 位置 |
|---|---|
| 评测样本集 | [`../evaluation/dataset/`](../evaluation/dataset/) |
| 评估方法说明文档 | `EVALUATION_METHOD.md`（本目录） |
| 评测脚本 | [`../evaluation/scripts/`](../evaluation/scripts/) |
| 完整结果表格 | `EVALUATION_RESULTS.md` |
| 有效性验证数据 | `VALIDATION.md` |
| 分析报告 | `ANALYSIS_REPORT.md` |
| Demo 视频 / GIF（≤2min） | `demos/` |

## 文档维护约定

- 「待回填」区域一律不写断言性结论，待评测实际运行后按脚本输出回填；
- 所有表格需标注采集时间与运行环境（provider / 模型 / 数据源可达性），保证可复现。
