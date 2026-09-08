# evaluation — 评测材料目录

本目录承载实战任务一「开放式场景：AI 应用与评判标准设计」的评测落地材料：

| 路径 | 说明 |
|---|---|
| `rubric.md` | 分维度评分细则（anchored rubric，1–5 分行为锚点），供 judge 与人工作标共用 |
| `dataset/` | 评测样本集（JSONL 种子 + 字段说明），构造细则见 `docs/EVALUATION_DATASET.md` |
| `scripts/` | 评测脚本：顺序跑一轮评测，输出 `results/` |

## 运行一轮评测

```bash
# 前置：本地服务已启动（python -m backend.main），且 .env 已配置 HY3_API_KEY
cd evaluation
python scripts/run_eval.py --dataset dataset/samples.jsonl --out results
```

输出：

- `results/raw/<sample_id>.json`：每次分析的完整报告（证据池 + 终态报告 + 执行指标）；
- `results/scores.json`：逐样本 6 维度得分、规则校验命中项与失败模式标签；
- 控制台汇总表格（供复制到 `docs/EVALUATION_RESULTS.md`）。

## 结果回填流程

1. 运行脚本 → `results/scores.json`；
2. 把汇总与逐样本数据整理进 `docs/EVALUATION_RESULTS.md`；
3. 用 `docs/VALIDATION.md` 的判别力/一致性实验设计做验证并把数据回填；
4. 在 `docs/ANALYSIS_REPORT.md` 中归纳失败模式与典型 case。

## 约定

- 本评测评估的是**分析产物质量**（信任度），方向对错由系统内置影子回测另行评估（见 `docs/EVALUATION_METHOD.md` §7）；
- 评测运行环境（时间、模型、数据源可达性）写入 `results/meta.json`，保证可复现；
- 任何结果填入 README/docs 时须来自脚本实际输出，不写未经运行的数据。
