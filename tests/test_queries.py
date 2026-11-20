"""GPU 时间戳查询的验收测试（初始全部为红）。

测试直接用内联 dict 构造池，不 mock 时间、不 sleep、不使用随机数；
所有时间都由显式 tick 驱动。tests 目录不可修改。
"""

import pytest

from timer.queries import (
    TimerError,
    aggregate,
    alloc_slot,
    begin,
    end,
    query_batch,
    resolve,
    reuse,
    stats,
)


def test_slot_is_reused_after_resolve():
    """缺陷 1：槽不复用，跑一天后池永远是满的。"""
    pool = {
        "capacity": 1,
        "cursor": 0,
        "slots": [
            {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
        ],
    }
    slot = alloc_slot(pool)
    begin(pool, slot, 10)
    end(pool, slot, 20)
    assert resolve(pool, 100)[0]["duration"] == 10
    assert reuse(pool, slot) == {"slot": slot, "state": "free"}
    assert alloc_slot(pool) == slot


def test_full_pool_alloc_returns_minus_one_without_overwrite():
    """缺陷 2：池满时静默返回 0，覆盖正在进行的查询。"""
    pool = {
        "capacity": 2,
        "cursor": 1,
        "slots": [
            {"state": "inflight", "begin_tick": 10, "end_tick": None, "result": None},
            {"state": "inflight", "begin_tick": 20, "end_tick": None, "result": None},
        ],
    }
    assert alloc_slot(pool) == -1
    assert pool["slots"][0]["begin_tick"] == 10
    assert pool["slots"][1]["begin_tick"] == 20


def test_duplicate_begin_raises():
    """缺陷 3：begin 不检查重复，同一槽两次 begin 数据错。"""
    pool = {
        "capacity": 1,
        "cursor": 0,
        "slots": [
            {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
        ],
    }
    slot = alloc_slot(pool)
    begin(pool, slot, 10)
    with pytest.raises(TimerError):
        begin(pool, slot, 12)


def test_end_before_begin_raises():
    """缺陷 4：end 不检查 tick 回退，负耗时。"""
    pool = {
        "capacity": 1,
        "cursor": 0,
        "slots": [
            {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
        ],
    }
    slot = alloc_slot(pool)
    begin(pool, slot, 50)
    with pytest.raises(TimerError):
        end(pool, slot, 49)


def test_resolve_skips_unfinished_query():
    """缺陷 5：resolve 直接返回未完成查询，耗时读成 0。"""
    pool = {
        "capacity": 1,
        "cursor": 0,
        "slots": [
            {"state": "inflight", "begin_tick": 10, "end_tick": None, "result": None},
        ],
    }
    assert resolve(pool, 1000) == []
    assert stats(pool) == {"inflight": 1, "free": 0, "resolved": 0}


def test_aggregate_ignores_unfinished_results():
    """缺陷 6：聚合把未完成查询算进平均，数值偏低。"""
    results = [
        {"slot": 0, "begin_tick": 100, "end_tick": 110, "duration": 10},
        {"slot": 1, "begin_tick": 100, "end_tick": None, "duration": None},
    ]
    out = aggregate(results, [(0, 100)])
    assert out == {0: {"min": 10, "max": 10, "avg": 10.0, "count": 1}}


def test_reuse_unresolved_slot_raises():
    """缺陷 7：复用未 resolve 的槽，结果丢失。"""
    pool = {
        "capacity": 1,
        "cursor": 0,
        "slots": [
            {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
        ],
    }
    slot = alloc_slot(pool)
    begin(pool, slot, 10)
    end(pool, slot, 12)
    with pytest.raises(TimerError):
        reuse(pool, slot)


def test_stats_sum_equals_capacity():
    """缺陷 8：统计三者之和不等于容量，出现空洞。"""
    pool = {
        "capacity": 4,
        "cursor": 3,
        "slots": [
            {"state": "inflight", "begin_tick": 10, "end_tick": 20, "result": None},
            {"state": "inflight", "begin_tick": 15, "end_tick": None, "result": None},
            {"state": "resolved", "begin_tick": 1, "end_tick": 2, "result": {"slot": 2}},
            {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
        ],
    }
    out = stats(pool)
    assert out == {"inflight": 2, "free": 1, "resolved": 1}
    assert sum(out.values()) == 4


def test_query_batch_keeps_pools_isolated():
    """缺陷 9：批量共用结果缓冲，池之间串。"""
    pools = {
        "gpu0": {
            "capacity": 3,
            "cursor": 0,
            "slots": [
                {"state": "inflight", "begin_tick": 10, "end_tick": 25, "result": None},
                {"state": "inflight", "begin_tick": 30, "end_tick": 31, "result": None},
                {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
            ],
        },
        "gpu1": {
            "capacity": 2,
            "cursor": 0,
            "slots": [
                {"state": "inflight", "begin_tick": 2, "end_tick": 4, "result": None},
                {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
            ],
        },
    }
    out = query_batch(pools, 100)
    assert [r["duration"] for r in out["gpu0"]] == [15, 1]
    assert [r["duration"] for r in out["gpu1"]] == [2]
    assert out["gpu0"] is not out["gpu1"]


def test_ring_index_wraps_without_overflow():
    """缺陷 10：环形池索引溢出，越界写。"""
    pool = {
        "capacity": 3,
        "cursor": 2,
        "slots": [
            {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
            {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
            {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
        ],
    }
    assert [alloc_slot(pool) for _ in range(3)] == [2, 0, 1]
    assert alloc_slot(pool) == -1


def test_alloc_empty_pool_returns_minus_one():
    """空池（没有任何空闲槽）alloc 返回 -1。"""
    pool = {
        "capacity": 2,
        "cursor": 1,
        "slots": [
            {"state": "inflight", "begin_tick": 5, "end_tick": None, "result": None},
            {
                "state": "resolved",
                "begin_tick": 1,
                "end_tick": 2,
                "result": {"slot": 1},
            },
        ],
    }
    assert alloc_slot(pool) == -1


def test_single_query_roundtrip_duration():
    """单查询完整往返：begin 100、end 115，耗时为 15 tick。"""
    pool = {
        "capacity": 1,
        "cursor": 0,
        "slots": [
            {"state": "free", "begin_tick": None, "end_tick": None, "result": None},
        ],
    }
    slot = alloc_slot(pool)
    begin(pool, slot, 100)
    end(pool, slot, 115)
    assert resolve(pool, 200) == [
        {"slot": 0, "begin_tick": 100, "end_tick": 115, "duration": 15}
    ]


def test_zero_capacity_pool_raises():
    """capacity=0 的池非法，分配时必须抛 TimerError。"""
    pool = {"capacity": 0, "cursor": 0, "slots": []}
    with pytest.raises(TimerError):
        alloc_slot(pool)


def test_resolve_orders_results_by_tick():
    """resolve 结果按 begin tick 升序，不受槽位顺序影响。"""
    pool = {
        "capacity": 3,
        "cursor": 0,
        "slots": [
            {"state": "inflight", "begin_tick": 30, "end_tick": 40, "result": None},
            {"state": "inflight", "begin_tick": 10, "end_tick": 12, "result": None},
            {"state": "inflight", "begin_tick": 20, "end_tick": 25, "result": None},
        ],
    }
    results = resolve(pool, 100)
    assert [r["begin_tick"] for r in results] == [10, 20, 30]
    assert [r["duration"] for r in results] == [2, 5, 10]


def test_query_batch_preserves_pool_order():
    """批量全部成功时，返回 dict 的池顺序与输入一致。"""
    pools = {
        "gpu0": {
            "capacity": 1,
            "cursor": 0,
            "slots": [
                {"state": "inflight", "begin_tick": 1, "end_tick": 3, "result": None},
            ],
        },
        "gpu1": {
            "capacity": 1,
            "cursor": 0,
            "slots": [
                {"state": "inflight", "begin_tick": 2, "end_tick": 6, "result": None},
            ],
        },
        "gpu2": {
            "capacity": 1,
            "cursor": 0,
            "slots": [
                {"state": "inflight", "begin_tick": 3, "end_tick": 9, "result": None},
            ],
        },
    }
    out = query_batch(pools, 100)
    assert list(out) == ["gpu0", "gpu1", "gpu2"]
    assert [out[name][0]["duration"] for name in out] == [2, 4, 6]
