# 评测脚手架

目的：用固定题库和固定指标，回答“这套流水线在同一道题上能拿到多少可核验证据”，而不是只看单篇报告观感。

## 运行

```powershell
# 校验题库
python cli.py eval --dry-run

# 先跑 3 题
python cli.py eval --limit 3

# 只跑指定题
python cli.py eval --only EVAL-01,EVAL-05

# 等价入口
python eval/run_eval.py --limit 3
```

结果写入 `eval/results/<run_id>/`：

- `metrics.json`：逐题原始指标
- `summary.md`：汇总表

`eval/results/` 已加入 `.gitignore`，避免把运行产物提交进仓库。

## 指标口径

| 指标 | 含义 |
|---|---|
| `elapsed_sec` | 单题从开始到产出报告的墙钟时间 |
| `materials` | 进入素材池的独立素材条数（去重后） |
| `claims_total` | 声明账本中的声明总数 |
| `claims_by_status` | `supported` / `unsupported` / `inferred` / `disputed` 分布 |
| `support_ratio` | `supported` 声明 / 全部声明，来自质量评分 |
| `gate_passed` / `gate_summary` | 确定性门禁结果（无来源、数值未命中、被质疑） |
| `quality_overall` | 工程自检综合分，不是事实正确率 |
| `cost_estimated` | 按配置常数估算的成本，不是真实账单 |
| `coverage_notes` | 素材覆盖不足信号（预算跳过、AI 补章、来源集中） |

## 与 GPT Researcher 同题对比

脚手架只负责本项目的指标采集。同题对比需要另外准备上游 GPT Researcher 环境，用相同的 `task` 跑一遍，把它的耗时、素材数、引用数和成本填进同一张表。

建议对比时固定：

- 相同模型与搜索接口；
- 相同页数或字数目标；
- 相同时间窗口（例如都取 2025 年数据）；
- 人工抽查同一批 10 条声明，判断来源是否真的支持结论。

## 解读注意

- `support_ratio` 只说明“数字能在素材原文找到”，不说明来源本身可信。
- `quality_overall` 可以被“多塞数字、多塞引用、章节等长”抬高，必须和 `gate_summary` 一起看。
- 单题失败不影响后续题目，失败原因写在 `metrics.json` 的 `error` 字段。
