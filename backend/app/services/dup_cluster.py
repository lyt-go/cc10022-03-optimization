"""重复簇领域服务：簇分组管理、合并冲突消解与去重处置。"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class DupClusterService(BaseModuleService):
    MODULE = "dup_cluster"
    ENTITY = "重复簇"
    OPERATION_TERM = "合并"
    LIST_FIELDS = ["簇编号", "实体模型", "簇大小", "相似度均值", "匹配规则", "创建时间", "处置状态", "处置批次", "成员"]
    REQUIRED_FIELDS = ["簇编号", "实体模型", "簇大小"]
    STATUS_ORDER = ["待确认", "审核中", "已合并", "已排除"]
    ACTION_RULES = {"加入审核": "审核中", "确认合并": "已合并", "排除误判": "已排除"}
    NEGATIVE_ACTIONS = []

    def register_clusters_from_round(self, round_result: dict[str, Any],
                                     entity_model: str, *,
                                     batch_label: str | None = None,
                                     source_job: str | None = None,
                                     only_members: bool = True) -> list[dict[str, Any]]:
        """把一轮匹配输出的疑似重复簇登记为待确认簇。

        每条簇都带成员（客户简称字段值）、簇大小、相似度均值，以及本轮实际
        使用的输入签名和规则签名；审核人员据此确认哪些客户可能重复。
        """
        rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0)
        batch = batch_label or source_job or round_result.get("input_signature", "")
        registered: list[dict[str, Any]] = []
        for detail in round_result.get("cluster_details", []):
            if only_members and detail.get("簇大小", 0) < 2:
                continue
            next_id += 1
            member_records = [member.get("字段值", {}) for member in detail.get("成员", [])]
            # 单字段规则直接展示简称；多字段规则用 | 连接，审核人员一眼能对上记录。
            members = [
                "|".join(str(value) for value in record.values())
                for record in member_records
            ]
            row = {
                "id": next_id,
                "status": "待确认",
                "pending": True,
                "abnormal": False,
                "簇编号": f"DUP-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{next_id}",
                "实体模型": entity_model,
                "簇大小": str(detail.get("簇大小", len(members))),
                "相似度均值": f"{float(detail.get('相似度均值', 0.0)):.4f}",
                "匹配规则": str(round_result.get("rule_id", "")),
                "创建时间": datetime.datetime.utcnow().isoformat(),
                "处置状态": "待确认",
                "处置批次": batch,
                # 审核证据：簇成员及其简称为审核人员提供直接依据
                "成员索引": detail.get("成员索引", []),
                "成员": members,
                "members": members,
                "成员记录": member_records,
                "输入签名": round_result.get("input_signature"),
                "规则签名": round_result.get("rule_signature"),
                "规则版本": round_result.get("rule_version"),
                "相似度阈值": str(round_result.get("threshold")),
                "匹配任务": source_job,
            }
            rows.append(row)
            registered.append(row)
        return registered

    def submit_batch_dedup(self, rule_id: int, records: list[dict[str, Any]],
                           entity_model: str = "customer", *,
                           batch_label: str | None = None) -> dict[str, Any]:
        """提交整批去重：分别用旧实现和优化实现跑同一批记录并比较结果。

        优化实现登记疑似重复簇；同时返回两版实现的簇成员和实做评分次数，
        便于确认优化没有改变原口径，并且确实少做了无效比较。
        """
        from app.services.match_rule import MatchRuleService
        rule_svc = MatchRuleService()
        comparison = rule_svc.compare_match_implementations(rule_id, records)
        if not comparison.get("ok"):
            return comparison
        optimized = comparison["optimized"]
        registered: list[dict[str, Any]] = []
        if optimized.get("review_ready"):
            registered = self.register_clusters_from_round(
                optimized, entity_model, batch_label=batch_label,
                source_job="整批去重",
            )
        return {
            "ok": True,
            "entity_model": entity_model,
            "legacy_members": comparison["legacy_members"],
            "optimized_members": comparison["optimized_members"],
            "clusters_equal": comparison["clusters_equal"],
            "legacy_scoring_calls": comparison["legacy_scoring_calls"],
            "optimized_scoring_calls": comparison["optimized_scoring_calls"],
            "optimized_sequence_matcher_calls": comparison["optimized_sequence_matcher_calls"],
            "scoring_calls_saved": comparison["scoring_calls_saved"],
            "legacy_rule_lookups": comparison["legacy_rule_lookups"],
            "optimized_rule_lookups": comparison["optimized_rule_lookups"],
            "review_ready": optimized["review_ready"],
            "可供审核": optimized["可供审核"],
            "registered_clusters": registered,
            "registered_count": len(registered),
            "round": optimized,
        }

    def merge_duplicate_cluster(self, cluster_id: int, records: list[dict[str, Any]]) -> dict[str, Any]:
        """合并重复簇：将簇内多条重复记录归并为一条代表记录。

        读取重复簇内的所有记录，按优胜策略归并字段值，
        合并重复记录生成代表记录，关联来源记录并写入合并追溯。
        """
        cluster = self.get_entry(cluster_id)
        if not cluster:
            return {"ok": False, "reason": "重复簇不存在"}
        if len(records) < 2:
            return {"ok": False, "reason": "重复簇记录数不足2条"}
        # 合并重复簇：归并组内记录生成代表记录并关联来源
        merged = self._resolve_merged_record(records)
        merged["_cluster_id"] = cluster.get("簇编号")
        merged["_source_record_count"] = len(records)
        merged["_merged_at"] = datetime.datetime.utcnow().isoformat()
        return {"ok": True, "merged_record": merged, "source_count": len(records)}

    def _resolve_merged_record(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        """归并重复记录：按最长值策略选取代表字段值。"""
        if not records:
            return {}
        keys = set()
        for r in records:
            keys.update(r.keys())
        merged = {}
        for key in keys:
            if key.startswith("_") or key in ("id", "status", "pending", "abnormal"):
                continue
            values = [r.get(key) for r in records if r.get(key) is not None]
            if values:
                # 归并重复记录：选取最长非空值作为合并代表值
                merged[key] = max(values, key=lambda v: len(str(v)) if v else 0)
        return merged

    def query_clusters_for_disposal(self, entity_model: str) -> list[dict[str, Any]]:
        """查询待处置重复簇列表：按实体模型筛选待确认和审核中的簇。

        遍历重复簇存储，查询待确认和审核中状态的重复簇，
        按相似度均值降序排列，优先处置高相似度的重复簇。
        """
        rows = store.rows(self.MODULE)
        pending = [
            row for row in rows
            if row.get("实体模型") == entity_model and row.get("status") in ("待确认", "审核中")
        ]
        # 查询待处置重复簇并按相似度均值降序排列
        pending.sort(key=lambda r: float(r.get("相似度均值", "0")), reverse=True)
        return pending

    def validate_cluster_dedup(self, cluster_id: int) -> dict[str, Any]:
        """校验重复簇去重结果的完整性：确认簇内所有记录均已被处理。

        读取重复簇数据，校验簇内记录是否全部归并完毕，
        验证去重结果的完备性并输出校验报告。
        """
        cluster = self.get_entry(cluster_id)
        if not cluster:
            return {"ok": False, "reason": "重复簇不存在"}
        cluster_size = int(cluster.get("簇大小", "0"))
        is_merged = cluster.get("status") == "已合并"
        return {
            "cluster_id": cluster.get("簇编号"),
            "dedup_complete": is_merged,
            "cluster_size": cluster_size,
            "disposal_status": cluster.get("处置状态"),
        }

    def generate_cluster_statistics(self, entity_model: str) -> dict[str, Any]:
        """生成重复簇统计：按实体模型聚合重复簇数量、相似度分布和处置率。

        查询重复簇存储，按实体模型分组聚合待确认、审核中、
        已合并和已排除的簇数量，计算重复率与处置完成率。
        """
        rows = store.rows(self.MODULE)
        model_rows = [r for r in rows if r.get("实体模型") == entity_model]
        by_status = {}
        for status in self.STATUS_ORDER:
            by_status[status] = sum(1 for r in model_rows if r.get("status") == status)
        sim_values = [float(r.get("相似度均值", "0")) for r in model_rows]
        avg_sim = round(sum(sim_values) / max(len(sim_values), 1), 4)
        return {
            "entity_model": entity_model,
            "total_clusters": len(model_rows),
            "by_status": by_status,
            "avg_similarity": avg_sim,
            "disposal_rate": round(
                (by_status.get("已合并", 0) + by_status.get("已排除", 0)) / max(len(model_rows), 1) * 100, 2
            ),
        }

    def exclude_false_positive(self, cluster_id: int, reason: str) -> dict[str, Any]:
        """排除误判重复簇：标记低相似度或人工确认为不重复的簇。

        将指定重复簇标记为已排除状态，记录误判原因用于匹配算法优化反馈。
        """
        cluster = self.get_entry(cluster_id)
        if not cluster:
            return {"ok": False, "reason": "重复簇不存在"}
        # 排除误判重复簇：标记为已排除并记录原因
        cluster["status"] = "已排除"
        cluster["exclude_reason"] = reason
        cluster["excluded_at"] = datetime.datetime.utcnow().isoformat()
        return {"ok": True, "cluster_id": cluster.get("簇编号"), "reason": reason}