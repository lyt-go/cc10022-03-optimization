"""abc / abcd / bcd 自动匹配 + 整批去重实操脚本。

用法（在 backend 目录）：
    .venv/bin/python -m app.tests.demo_match_optimization

输出内容
--------
1. 一轮自动匹配：簇成员、规则/输入签名、可供审核标记、实做评分次数。
2. 整批去重：旧实现与新实现的簇成员对照、两边实做评分次数。
3. 运行中更正阈值：任务里实际保留的规则快照与审核可用性。
"""
from __future__ import annotations

import json
import os
import sys

_backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)

from app.services.auto_match_job import AutoMatchJobService
from app.services.dup_cluster import DupClusterService
from app.services.match_rule import MatchRuleService
from app.store import store

FIELD = "客户简称"
RECORDS = [
    {"id": 1, FIELD: "abc"},
    {"id": 2, FIELD: "abcd"},
    {"id": 3, FIELD: "bcd"},
]


def make_rule(threshold: str, rule_code: str) -> int:
    rows = store.rows("match_rule")
    rule_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
    rows.append({
        "id": rule_id, "status": "已生效", "pending": False, "abnormal": False,
        "规则编号": rule_code, "实体模型": "customer", "匹配字段": FIELD,
        "匹配算法": "fuzzy", "相似度阈值": threshold, "模糊系数": "0.85",
        "权重分配": "1", "规则版本": "v1",
    })
    return rule_id


def main() -> None:
    rule_id = make_rule("0.7286", "MATC-DEMO-001")
    match_svc = MatchRuleService()

    print("== 两两相似度（原公式）==")
    labels = [record[FIELD] for record in RECORDS]
    for i in range(len(RECORDS)):
        for j in range(i + 1, len(RECORDS)):
            score = match_svc.compute_similarity(rule_id, RECORDS[i], RECORDS[j])
            print(f"  {labels[i]}~{labels[j]} = {score}")
    print("  说明: abc~bcd 低于阈值 0.7286，但 abc~abcd、abcd~bcd 达标，经 abcd 传递成簇")

    print("\n== 1. 一轮自动匹配 ==")
    job = AutoMatchJobService().schedule_match_job(
        rule_id, "customer", RECORDS, cluster_batch_label="DEMO-AUTO"
    )
    row = job["job"]
    print(json.dumps({
        "簇成员（下标）": job["clusters"],
        "簇成员（简称）": [
            [member["字段值"][FIELD] for member in detail["成员"]]
            for detail in job["cluster_details"]
        ],
        "输出簇数": row["输出簇数"],
        "登记簇数": row["登记簇数"],
        "规则签名": row["rule_signature"],
        "输入签名": row["input_signature"],
        "实际使用阈值": row["threshold_used"],
        "可供审核": row["可供审核"],
        "实做评分次数": row["scored_pairs"],
        "总组合数": row["total_pairs"],
        "剪枝组合数": row["pruned_pairs"],
        "规则解析次数": row["rule_lookups"],
    }, ensure_ascii=False, indent=2))

    print("\n== 2. 随后提交整批去重：旧实现 vs 优化实现 ==")
    dedup = DupClusterService().submit_batch_dedup(rule_id, RECORDS, "customer",
                                                   batch_label="DEMO-DEDUP")
    print(json.dumps({
        "旧实现成员": dedup["legacy_members"],
        "优化实现成员": dedup["optimized_members"],
        "成员一致": dedup["clusters_equal"],
        "旧实现实做评分次数": dedup["legacy_scoring_calls"],
        "优化实现实做评分次数": dedup["optimized_scoring_calls"],
        "少做的评分次数": dedup["scoring_calls_saved"],
        "旧实现规则解析次数": dedup["legacy_rule_lookups"],
        "优化实现规则解析次数": dedup["optimized_rule_lookups"],
        "新登记簇数": dedup["registered_count"],
    }, ensure_ascii=False, indent=2))

    print("\n== 3. 运行中更正阈值（0.7286 -> 0.9）==")
    rule_id2 = make_rule("0.7286", "MATC-DEMO-002")
    changed = {}

    def on_pair_start(i: int, j: int) -> None:
        if not changed:
            match_svc.update_rule_threshold(rule_id2, "0.9")
            changed["signature"] = match_svc.rule_signature(match_svc.get_entry(rule_id2))

    result = AutoMatchJobService().schedule_match_job(
        rule_id2, "customer", RECORDS, on_pair_start=on_pair_start,
        cluster_batch_label="DEMO-CHANGED",
    )
    job2 = result["job"]
    print(json.dumps({
        "任务里保留的规则阈值": job2["rule_snapshot"]["相似度阈值"],
        "规则更正后的阈值": match_svc.get_entry(rule_id2)["相似度阈值"],
        "本轮实际使用阈值": job2["threshold_used"],
        "本轮簇成员": result["clusters"],
        "规则在计算中是否变更": job2["rule_changed"],
        "可供审核": job2["可供审核"],
        "审核结论": job2["审核结论"],
        "登记簇数": job2["登记簇数"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
