"""批量匹配优化测试：口径不变、少做无效比较、运行中更正阈值不混用参数。

覆盖场景
--------
1. abc / abcd / bcd 一轮自动匹配：三边分别 0.7286 / 0.5667 / 0.7286，只有
   0.7286 达到阈值，但两条达标边把三条记录连成一个传递簇。
2. 同一批记录再走“整批去重”，对照旧实现与新实现的簇成员和实做评分次数。
3. 运行中把阈值从 0.7286 改到 0.9：本轮仍按开始时的快照计算并输出簇，
   但结果被标记为不可审核；并发场景下任务数据只保留开始时那一份规则。
"""
from __future__ import annotations

import os
import sys
import threading

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.auto_match_job import AutoMatchJobService
from app.services.dup_cluster import DupClusterService
from app.services.match_rule import MatchRuleService
from app.store import store

CUSTOMER_FIELD = "客户简称"


def _make_rule(threshold: str, *, rule_code: str) -> int:
    """在匹配规则存储中新建一条按客户简称模糊匹配的规则，返回规则 id。"""
    rows = store.rows("match_rule")
    rule_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    rows.append({
        "id": rule_id,
        "status": "已生效",
        "pending": False,
        "abnormal": False,
        "规则编号": rule_code,
        "实体模型": "customer",
        "匹配字段": CUSTOMER_FIELD,
        "匹配算法": "fuzzy",
        "相似度阈值": threshold,
        "模糊系数": "0.85",
        "权重分配": "1",
        "规则版本": "v1",
    })
    return rule_id


def _reset_cluster_rows() -> int:
    rows = store.rows("dup_cluster")
    baseline = max((int(r.get("id", 0)) for r in rows), default=0)
    return baseline


def test_abc_bcd_round_and_batch_dedup():
    """abc / abcd / bcd：一次自动匹配 + 整批去重，核对成员与实做评分次数。"""
    rule_id = _make_rule("0.7286", rule_code="MATC-ABC-001")
    records = [
        {"id": 1, CUSTOMER_FIELD: "abc"},
        {"id": 2, CUSTOMER_FIELD: "abcd"},
        {"id": 3, CUSTOMER_FIELD: "bcd"},
    ]
    rule_svc = MatchRuleService()

    # 三对分数：abc~abcd=0.7286（临界，达标）、abcd~bcd=0.7286（达标）、abc~bcd=0.5667。
    assert rule_svc.compute_similarity(rule_id, records[0], records[1]) == 0.7286
    assert rule_svc.compute_similarity(rule_id, records[1], records[2]) == 0.7286
    assert rule_svc.compute_similarity(rule_id, records[0], records[2]) == 0.5667

    baseline = _reset_cluster_rows()
    job_svc = AutoMatchJobService()
    job = job_svc.schedule_match_job(rule_id, "customer", records)
    assert job["ok"] is True, job
    assert job["clusters"] == [[0, 1, 2]], f"三条记录应经传递连边同簇: {job['clusters']}"
    assert job["job"]["输出簇数"] == "1"
    assert job["job"]["登记簇数"] == "1"
    assert job["job"]["可供审核"] == "可供审核"
    assert job["job"]["rule_signature"], "任务必须记录实际使用的规则签名"
    assert job["job"]["input_signature"], "任务必须记录实际输入签名"

    # 簇成员登记到疑似重复簇：成员简称与簇大小可供审核人员直接核对。
    cluster_rows = [r for r in store.rows("dup_cluster") if int(r["id"]) > baseline]
    assert len(cluster_rows) == 1
    cluster = cluster_rows[0]
    assert cluster["簇大小"] == "3"
    assert sorted(cluster["成员"]) == ["abc", "abcd", "bcd"]
    assert cluster["规则签名"] == job["job"]["rule_signature"]
    assert cluster["输入签名"] == job["job"]["input_signature"]

    # 随后再提交整批去重：旧实现与优化后的成员完全一致，评分次数明显更少。
    dedup = DupClusterService().submit_batch_dedup(rule_id, records, "customer")
    assert dedup["ok"] is True
    assert dedup["clusters_equal"] is True
    assert dedup["legacy_members"] == dedup["optimized_members"] == [[0, 1, 2]]
    # 旧实现在 3 条记录上做 3 次两两评分；优化后靠上界剪掉 abc~bcd（0.5667 < 阈值）。
    assert dedup["legacy_scoring_calls"] == 3
    assert dedup["optimized_scoring_calls"] == 2
    assert dedup["scoring_calls_saved"] == 1
    # 旧实现每个记录对重新解析一次规则；优化后整轮只解析一次。
    assert dedup["legacy_rule_lookups"] == 3
    assert dedup["optimized_rule_lookups"] == 1
    assert dedup["registered_count"] == 1
    return dedup


def test_pruning_never_changes_members_on_larger_batch():
    """较大输入上做随机对照：优化前后簇成员必须完全一致。"""
    rule_id = _make_rule("0.62", rule_code="MATC-ABC-002")
    names = ["abc", "abcd", "bcd", "ab", "bcdx", "xabc", "abd", "bdc", "cd", "abcdb"]
    records = [{"id": i + 1, CUSTOMER_FIELD: name} for i, name in enumerate(names)]
    comparison = MatchRuleService().compare_match_implementations(rule_id, records)
    assert comparison["clusters_equal"] is True, (
        f"优化改变了簇成员: 旧 {comparison['legacy_members']} / 新 {comparison['optimized_members']}"
    )
    assert comparison["optimized_scoring_calls"] < comparison["legacy_scoring_calls"]
    assert comparison["optimized_rule_lookups"] == 1
    return comparison


def test_threshold_change_during_round_is_not_mixed():
    """运行中更正阈值：本轮只用开始时那份规则，并标记为不可审核。"""
    rule_id = _make_rule("0.7286", rule_code="MATC-ABC-003")
    records = [
        {"id": 1, CUSTOMER_FIELD: "abc"},
        {"id": 2, CUSTOMER_FIELD: "abcd"},
        {"id": 3, CUSTOMER_FIELD: "bcd"},
    ]
    rule_svc = MatchRuleService()
    job_svc = AutoMatchJobService()
    changed = {}

    def on_pair_start(i: int, j: int) -> None:
        # 在第一次真正开始评分之前更正阈值，模拟运营人员的并发操作。
        if not changed:
            rule_svc.update_rule_threshold(rule_id, "0.9")
            changed["signature"] = rule_svc.rule_signature(rule_svc.get_entry(rule_id))

    result = job_svc.schedule_match_job(
        rule_id, "customer", records, on_pair_start=on_pair_start
    )
    assert result["ok"] is True
    job = result["job"]

    # 本轮任务数据保留的是开始时的阈值快照，不是更正后的 0.9。
    assert job["rule_snapshot"]["相似度阈值"] == "0.7286"
    assert job["threshold_used"] == 0.7286
    assert job["rule_signature"] != job["current_rule_signature"]
    assert job["rule_signature"] == changed["signature"] or job["rule_changed"] is True
    # 参数没有被混用：簇仍按 0.7286 的规则生成，但审核人员不能拿它当审核依据。
    assert result["clusters"] == [[0, 1, 2]]
    assert job["可供审核"] == "不可审核"
    assert job["review_ready"] is False
    assert job["abnormal"] is True
    assert "规则" in job["review_status"]
    # 不可审核的一轮不登记待确认簇，避免审核名单混入过期口径的成员。
    assert job["登记簇数"] == "0"
    assert job["registered_cluster_ids"] == []

    # 审核名单接口明确拒绝并说明原因。
    manifest = job_svc.review_cluster_manifest(job["id"])
    assert manifest["ok"] is False
    assert manifest["review_ready"] is False
    assert "规则" in manifest["reason"]

    # 更正后的规则对下一轮生效：同样的记录按 0.9 不再成簇。
    second = job_svc.schedule_match_job(rule_id, "customer", records)
    assert second["clusters"] == []
    assert second["job"]["可供审核"] == "可供审核"
    return job


def test_task_keeps_rule_snapshot_under_real_concurrency():
    """真实并发：计算线程持有开始时快照，更正线程改规则，任务数据只保留旧参数。"""
    rule_id = _make_rule("0.7286", rule_code="MATC-ABC-004")
    names = ["abc", "abcd", "bcd", "ab", "bcdx", "xabc", "abd", "bdc", "cd", "abcdb"]
    records = [{"id": i + 1, CUSTOMER_FIELD: name} for i, name in enumerate(names)]
    rule_svc = MatchRuleService()
    job_holder: dict[str, object] = {}
    started = threading.Event()
    updated = threading.Event()

    def on_pair_start(i: int, j: int) -> None:
        # 第一对开始评分时通知更正线程；等它改完再继续，保证更正发生在“一轮计算中”。
        if not started.is_set():
            started.set()
            updated.wait(timeout=10)

    def run_job() -> None:
        job_holder["result"] = AutoMatchJobService().schedule_match_job(
            rule_id, "customer", records, on_pair_start=on_pair_start
        )

    def correct_rule() -> None:
        started.wait(timeout=10)
        # 计算进行期间反复更正阈值，制造与运行中请求的竞争。
        for threshold in ("0.9", "0.95", "0.5", "0.99"):
            rule_svc.update_rule_threshold(rule_id, threshold)
        updated.set()

    job_thread = threading.Thread(target=run_job)
    rule_thread = threading.Thread(target=correct_rule)
    job_thread.start()
    rule_thread.start()
    job_thread.join(timeout=30)
    rule_thread.join(timeout=30)

    result = job_holder["result"]
    assert result["ok"] is True
    job = result["job"]
    # 任务里只保留实际参与计算的那份阈值，绝不会出现 0.9/0.95/0.5 等更正后的值。
    assert job["rule_snapshot"]["相似度阈值"] == "0.7286"
    assert job["threshold_used"] == 0.7286
    assert job["rule_signature"] == MatchRuleService.rule_signature({
        **{k: v for k, v in job["rule_snapshot"].items()},
    })
    assert job["rule_changed"] is True
    assert job["review_ready"] is False
    assert job["可供审核"] == "不可审核"
    return job


def test_input_snapshot_survives_mutation():
    """调用方在一轮计算中修改 records，本轮输出仍基于实际提交的输入快照。"""
    rule_id = _make_rule("0.7286", rule_code="MATC-ABC-005")
    records = [
        {"id": 1, CUSTOMER_FIELD: "abc"},
        {"id": 2, CUSTOMER_FIELD: "abcd"},
        {"id": 3, CUSTOMER_FIELD: "bcd"},
    ]
    result = MatchRuleService().run_match_round(rule_id, records)
    records[0][CUSTOMER_FIELD] = "zzz"
    stored = result["输入快照"]["记录"]
    assert [r[CUSTOMER_FIELD] for r in stored] == ["abc", "abcd", "bcd"]
    assert result["input_signature"] == MatchRuleService.input_signature(stored)
    return result


def test_legacy_compute_similarity_signature_still_supported():
    """单独调用 compute_similarity 的旧用法保持不变。"""
    rule_id = _make_rule("0.7", rule_code="MATC-ABC-006")
    record_a = {CUSTOMER_FIELD: "abc"}
    record_b = {CUSTOMER_FIELD: "abcd"}
    assert MatchRuleService().compute_similarity(rule_id, record_a, record_b) == 0.7286
    assert MatchRuleService().generate_duplicate_clusters(rule_id, [record_a, record_b]) == [[0, 1]]


def test_random_inputs_match_legacy_clusters():
    """随机简称与阈值下做差分：剪枝只能减少评分，不能改变任何簇成员。"""
    import random

    rng = random.Random(20261121)
    alphabet = "abcd"
    rule_svc = MatchRuleService()
    for round_index in range(40):
        rule_id = _make_rule(
            f"{rng.uniform(0.4, 0.95):.4f}", rule_code=f"MATC-FUZZ-{round_index:03d}"
        )
        records = [
            {"id": index + 1, CUSTOMER_FIELD: "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 7)))}
            for index in range(rng.randint(2, 14))
        ]
        comparison = rule_svc.compare_match_implementations(rule_id, records)
        assert comparison["clusters_equal"] is True, (
            f"第 {round_index} 轮剪枝改变了簇成员: 阈值 "
            f"{rule_svc.get_entry(rule_id)['相似度阈值']}\n"
            f"旧 {comparison['legacy_members']} / 新 {comparison['optimized_members']}"
        )
        assert comparison["optimized_scoring_calls"] <= comparison["legacy_scoring_calls"]
        assert comparison["optimized_rule_lookups"] == 1


def test_boundary_score_is_inclusive():
    """临界分数（恰好等于阈值）在优化实现下仍然算达标。"""
    rule_id = _make_rule("0.7286", rule_code="MATC-BOUNDARY-001")
    records = [{"id": 1, CUSTOMER_FIELD: "abc"}, {"id": 2, CUSTOMER_FIELD: "abcd"}]
    result = MatchRuleService().run_match_round(rule_id, records)
    assert result["clusters"] == [[0, 1]]
    assert result["scored_pairs"] == 1
    # 再抬高四舍五入末位，0.7286 不再达标，两侧都应当相同。
    MatchRuleService().update_rule_threshold(rule_id, "0.7287")
    tightened = MatchRuleService().run_match_round(rule_id, records)
    assert tightened["clusters"] == []


if __name__ == "__main__":
    checks = [
        ("abc/bcd 一轮匹配 + 整批去重", test_abc_bcd_round_and_batch_dedup),
        ("上界剪枝不改变簇成员", test_pruning_never_changes_members_on_larger_batch),
        ("随机输入下与旧实现簇成员一致", test_random_inputs_match_legacy_clusters),
        ("临界分数仍算达标", test_boundary_score_is_inclusive),
        ("运行中更正阈值不混用参数", test_threshold_change_during_round_is_not_mixed),
        ("并发更正下任务只保留开始时规则", test_task_keeps_rule_snapshot_under_real_concurrency),
        ("输入快照不被调用方修改影响", test_input_snapshot_survives_mutation),
        ("compute_similarity 旧用法兼容", test_legacy_compute_similarity_signature_still_supported),
    ]
    passed = failed = 0
    for name, fn in checks:
        try:
            fn()
            print(f"  PASS: {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001 - 命令行自检需要打印失败原因
            print(f"  FAIL: {name} -> {exc}")
            failed += 1
    print(f"\n结果: {passed}/{len(checks)} 通过, {failed} 失败")
    assert failed == 0, f"{failed} 条批量匹配测试失败！"
