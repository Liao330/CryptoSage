# evaluation/scripts — 评测脚本

## 依赖

复用项目后端依赖即可：`httpx`、`openai`（pip install -r requirements.txt）。

## 运行

```bash
# 0) 确保后端已启动
#    python -m backend.main

# 1) 在仓库根目录 .env 配置 judge 用 Key（默认复用 HY3_API_KEY / HY3_BASE_URL）
#    （可选）EVAL_JUDGE_MODEL=hy3-slow  覆盖 judge 模型

# 2) 运行评测（在仓库根目录）
python evaluation/scripts/run_eval.py \
  --dataset evaluation/dataset/samples.jsonl \
  --out evaluation/results \
  --api http://127.0.0.1:8000
```

## 输出

| 路径 | 内容 |
|---|---|
| `results/meta.json` | 运行环境（时间、样本数、模型、API） |
| `results/raw/<id>.json` | 每次分析的服务端完整响应（含 report / trace / metrics） |
| `results/scores.json` | 逐样本：6 维得分、规则校验命中、失败模式标签、judge 说明 |
| 控制台 | 汇总表（可复制入 `docs/EVALUATION_RESULTS.md`） |

## 说明与限制（重要）

- 本脚本是**示例实现**：单 judge（Hy3）逐维度打分 + 规则校验 + 标签。`EVALUATION_METHOD.md` 中描述的"双 judge + 仲裁"可通过设置 `EVAL_JUDGE_SECOND_MODEL`（如 `deepseek-v4-pro`，并配置 `DEEPSEEK_API_KEY`）启用，分歧 >1 分时自动用主 judge 仲裁；
- 评测质量依赖 rubric 文本与样本构造，脚本本身不做真假判断；
- 一次样本若后端返回 error/超时，`scores.json` 中该样本标记 `run_error`，避免静默吞掉失败；
- 脚本会并发 1 个任务（后端默认并发上限 4，需要更高吞吐可传 `--workers 2` 并配合 `MAX_CONCURRENT_ANALYSES`）。
