# GPT Researcher 内置源码说明

本目录是上游 [GPT Researcher](https://github.com/assafelovic/gpt-researcher) 的 vendored 快照，直接放进仓库随项目分发。

## 当前状态

- **没有版本号**：目录里没有 `__version__`、`VERSION` 或来源 commit 记录。
- **运行时优先使用本目录**：`gptr_service/main.py` 把 `third_party/gpt_researcher` 插入 `sys.path` 首位，因此它优先于 pip 安装的 `gpt-researcher`。
- **pip 依赖只作兜底**：`gptr_service/requirements.txt` 仍声明 `gpt-researcher>=0.13.0`，在本目录缺失时才会用到。
- 与上游的差异没有单独记录；`docs/WIKI.md` 中的“带补丁”指本目录相对上游有修改，但未保留 diff。

这份说明只解决一个问题：将来升级时，知道该核对什么，而不是重新猜一遍。

## gptr_service 依赖的接口

升级后至少确认下面这些仍然成立（见 `gptr_service/core/worker.py`）：

```python
from gpt_researcher import GPTResearcher

researcher = GPTResearcher(query=..., report_type=..., tone=...)
text = await researcher.conduct_research()
```

以及配置注入方式：

- `researcher.cfg.smart_llm_model`
- `researcher.cfg.fast_llm_model`
- `researcher.cfg.llm_provider` / `smart_llm_provider` / `fast_llm_provider`
- `researcher.cfg.openai_api_key`
- `researcher.cfg.openai_api_base`

## 升级步骤

1. 备份：把当前目录复制到仓库外，或先打一个 git tag。
2. 获取上游目标版本，记录 tag / commit / 日期。
3. 替换 `third_party/gpt_researcher/gpt_researcher/` 内容，保留本文件。
4. 对照上面的接口做兼容检查，重点看构造参数、`conduct_research()` 返回值和 `cfg` 字段名。
5. 跑单元测试：

   ```powershell
   python 调研工具/test_engine.py --quick
   python 调研工具/test_regression.py
   ```

6. 启动 `gptr_service`，确认健康检查通过。
7. 跑一题小样本，核对素材数、引用编号和错误日志：

   ```powershell
   python cli.py eval --only EVAL-01 --pages 8 --no-deliver
   ```

8. 在下方版本记录表登记结果。

## 已知风险

- 上游可能改动 `GPTResearcher` 构造参数或配置字段，`worker.py` 会直接报错。
- 本目录无版本标识，运行日志无法打印当前 GPTR 版本；升级时可顺手补一个 `__version__`。
- `requirements.txt` 的 `gpt-researcher>=0.13.0` 与本目录可能不一致，升级后要同步。
- 本目录带本地补丁，直接覆盖会丢失；替换前先确认哪些文件被改过（当前没有 diff 记录，只能靠人工比对）。

## 版本记录

| 日期 | 上游版本 / commit | 操作 | 验证结果 |
|---|---|---|---|
| 待补 | 未知（初始快照） | 导入 | 四套测试通过 |
