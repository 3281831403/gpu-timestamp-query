# GPU 时间戳查询（gpu-timestamp-query）

## 题面

接入 GPU 计时后，性能面板跑一会儿就全是 0、耗时还算出负数：槽不复用，跑一天后池永远是满的；池满时静默返回 0，覆盖正在进行的查询；begin 不检查重复，同一槽两次 begin 数据错；end 不检查 tick 回退，负耗时；resolve 直接返回未完成查询，耗时读成 0；聚合把未完成查询算进平均，数值偏低；复用未 resolve 的槽，结果丢失；统计三者之和不等于容量，出现空洞；批量共用结果缓冲，池之间串；环形池索引溢出，越界写。timer/queries.py 是核心，tests/test_queries.py 15个红。要修的是：严格实现 tick 契约与 `free → inflight → resolved` 生命周期，补上容量、重复 begin、tick 回退与 reuse 前置校验，只回读已结束查询并按 tick 升序返回，聚合只统计已完成结果，统计三个计数无空洞，批量各池结果相互隔离且任一池非法时整体回滚，最终让 15 个测试全绿。

## 仓库结构

```
gpu-timestamp-query/
├── README.md
├── conftest.py            # 把仓库根加入 sys.path
├── .gitignore
├── timer/
│   ├── __init__.py
│   └── queries.py         # 8 个待实现函数 + TimerError
└── tests/
    └── test_queries.py    # 15 个验收测试（不可修改）
```

`timer/queries.py` 中的 8 个函数目前全部 `raise NotImplementedError`：

| 函数 | 契约摘要 |
| --- | --- |
| `alloc_slot(pool) -> int` | 从环形池取空闲槽；池满返回 `-1`；`capacity <= 0` 抛 `TimerError` |
| `begin(pool, slot, tick) -> dict` | 记录开始 tick；重复 begin 抛 `TimerError` |
| `end(pool, slot, tick) -> dict` | 记录结束 tick；tick 回退抛 `TimerError`，不产生负耗时 |
| `resolve(pool, available_until) -> list[dict]` | 只回读已结束查询，按 begin tick 升序；回读边界有 1 tick 容差 |
| `aggregate(results, buckets) -> dict` | 按桶统计 min/max/avg，空桶全为 0，未完成结果不计入 |
| `reuse(pool, slot) -> dict` | 仅已 resolve 的槽可复用，否则抛 `TimerError` |
| `stats(pool) -> dict` | 返回 `{"inflight", "free", "resolved"}`，三者之和等于容量 |
| `query_batch(pools, available_until) -> dict` | 批量回读；各池结果隔离，任一池非法时整体回滚 |

## 运行方式

在仓库根目录执行：

```bash
python -m pytest tests -q
```

初始（修复前）输出应为恰好 **15 failed**，且没有 collection error、import error 或 syntax error。实现完成后同样的命令应显示 15 passed。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 图形渲染 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 约束

- 只使用 Python 标准库（`bisect`、`collections`）与 pytest；离线、无随机、不 sleep，时间一律用显式 tick。
- `timer/queries.py` 是唯一需要修改的核心文件。
- `tests/test_queries.py` 不可修改；测试中的池均为内联 dict，15 个用例覆盖 10 个缺陷与 5 个组合场景。
