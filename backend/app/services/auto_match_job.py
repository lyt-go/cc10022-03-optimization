"""自动匹配任务领域服务：匹配任务调度、并行匹配执行与簇结果输出。

任务与一轮匹配的绑定关系
------------------------
* 每轮匹配都冻结规则快照和输入快照，任务行同时保存规则签名与输入签名，
  因此可以从任务输出直接看出这一轮到底按什么规则、对什么数据计算。
* 计算结束后比对当前规则指纹：运行期间运营人员更正过阈值，任务结果仍然
  保留开始时那份快照，但会标记为不可审核，审核人员不会拿到混用参数的簇。
* 输出疑似重复簇时，会把簇内成员和簇数登记到重复簇模块，审核人员据此
  确认哪些客户可能重复。
"""
from __future__ import annotations

import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class AutoMatchJobService(BaseModuleService):
    MODULE = "auto_match_job"
    ENTITY = "匹配任务"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["任务编号", "实体模型", "匹配规则", "数据范围", "并行度", "匹配策略", "输出簇数", "任务状态", "可供审核"]
    REQUIRED_FIELDS = ["任务编号", "实体模型", "匹配规则"]
    STATUS_ORDER = ["草稿", "等待中", "运行中", "已完成", "已失败"]
    ACTION_RULES = {"提交运行": "草稿", "停止匹配": "运行中", "确认完成": "已完成"}
    NEGATIVE_ACTIONS = []

    # 审核可用性取值：任务输出里直接携带，供审核名单的页面判断。
    REVIEW_READY = "可供审核"
    REVIEW_BLOCKED = "不可审核"

    def schedule_match_job(self, rule_id: int, entity_model: str,
                            records: list[dict[str, Any]], *,
                            on_pair_start: Any = None,
                            cluster_batch_label: str | None = None) -> dict[str, Any]:
        """调度匹配任务：按匹配规则对实体模型记录执行自动匹配和去重检测。

        读取匹配规则配置，调度自动匹配任务计算记录对的模糊相似度，
        校验阈值并生成疑似重复簇输出，返回任务执行统计。
        任务行会保存本轮实际使用的规则快照、输入快照和可供审核标记。
        """
        from app.services.match_rule import MatchRuleService
        rule_svc = MatchRuleService()
        rule = rule_svc.get_entry(rule_id)
        if not rule:
            return {"ok": False, "reason": "匹配规则不存在"}
        # 调度匹配任务：读取匹配规则，执行模糊匹配计算和去重检测
        round_result = rule_svc.run_match_round(
            rule_id, records, on_pair_start=on_pair_start
        )
        if round_result is None:
            return {"ok": False, "reason": "匹配规则不存在"}

        clusters = round_result["clusters"]
        cluster_details = round_result["cluster_details"]
        review_ready = round_result["review_ready"]
        verdict = self.REVIEW_READY if review_ready else self.REVIEW_BLOCKED
        task_id = f"TASK-{datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}"
        job_result = {
            "task_id": task_id,
            "entity_model": entity_model,
            "rule_id": rule_id,
            "total_records": round_result["input_count"],
            "clusters_found": len(clusters),
            "records_in_clusters": sum(len(c) for c in clusters),
            "match_strategy": rule.get("匹配策略", "全量匹配"),
            "completed_at": datetime.datetime.utcnow().isoformat(),
        }
        # 写入匹配任务结果并存储疑似重复簇
        rows = store.rows(self.MODULE)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "任务编号": task_id,
            "实体模型": entity_model,
            "匹配规则": str(rule_id),
            "并行度": "4",
            "匹配策略": job_result["match_strategy"],
            "输出簇数": str(job_result["clusters_found"]),
            "任务状态": "已完成",
            "status": "已完成",
            "pending": False,
            "abnormal": False,
            "clusters": clusters,
            "簇明细": cluster_details,
            "cluster_details": cluster_details,
            "规则快照": round_result["规则快照"],
            "rule_snapshot": round_result["rule_snapshot"],
            "rule_signature": round_result["rule_signature"],
            "current_rule_signature": round_result["current_rule_signature"],
            "rule_changed": round_result["rule_changed"],
            "input_signature": round_result["input_signature"],
            "输入快照": round_result["输入快照"],
            "threshold_used": round_result["threshold"],
            "match_fields": round_result["match_fields"],
            "scored_pairs": round_result["scored_pairs"],
            "pruned_pairs": round_result["pruned_pairs"],
            "candidate_pairs": round_result["candidate_pairs"],
            "total_pairs": round_result["total_pairs"],
            "sequence_matcher_calls": round_result["sequence_matcher_calls"],
            "rule_lookups": round_result["rule_lookups"],
            "可供审核": verdict,
            "review_ready": review_ready,
            "review_status": round_result["review_status"],
            "审核结论": round_result["审核结论"],
        }
        if not review_ready:
            entry["abnormal"] = True
            entry["review_blocked_reason"] = "规则在计算期间已变更，本轮簇按开始时的规则快照生成"
        rows.append(entry)

        # 把簇成员和簇数登记到重复簇模块，审核人员据此确认哪些客户可能重复。
        registered = []
        if review_ready and clusters:
            from app.services.dup_cluster import DupClusterService
            registered = DupClusterService().register_clusters_from_round(
                round_result, entity_model,
                batch_label=cluster_batch_label,
                source_job=task_id,
            )
        entry["登记簇数"] = str(len(registered))
        entry["registered_cluster_ids"] = [row.get("id") for row in registered]

        return {"ok": True, "job": entry, "clusters": clusters,
                "cluster_details": cluster_details, "round": round_result,
                "registered_clusters": registered}

    def query_job_status(self, job_id: int) -> dict[str, Any] | None:
        """查询匹配任务的执行状态与输出簇数统计。

        读取匹配任务存储中的指定任务，查询任务状态、输出簇数、
        并行度和匹配策略信息，返回任务详细状态；同时给出本轮实际使用的
        规则/输入快照、实做评分次数以及是否可供审核。
        """
        job = self.get_entry(job_id)
        if not job:
            return None
        review_ready = job.get("review_ready")
        if review_ready is None:
            review_ready = self._review_ready(job)
        # 查询匹配任务状态：读取任务执行状态与输出簇数统计
        return {
            "task_id": job.get("任务编号"),
            "status": job.get("任务状态"),
            "entity_model": job.get("实体模型"),
            "clusters_output": int(job.get("输出簇数", "0")),
            "parallelism": int(job.get("并行度", "1")),
            "strategy": job.get("匹配策略"),
            "rule_id": job.get("匹配规则"),
            "rule_snapshot": job.get("rule_snapshot"),
            "规则快照": job.get("规则快照"),
            "rule_signature": job.get("rule_signature"),
            "input_signature": job.get("input_signature"),
            "输入快照": job.get("输入快照"),
            "threshold_used": job.get("threshold_used"),
            "scored_pairs": job.get("scored_pairs"),
            "total_pairs": job.get("total_pairs"),
            "review_ready": bool(review_ready),
            "可供审核": job.get("可供审核", self.REVIEW_READY if review_ready else self.REVIEW_BLOCKED),
            "review_status": job.get("review_status"),
            "registered_cluster_ids": job.get("registered_cluster_ids", []),
        }

    def _review_ready(self, job: dict[str, Any]) -> bool:
        """根据任务输出判断是否可供审核：规则签名发生变化即视为不可审核。"""
        from app.services.match_rule import MatchRuleService
        rule_svc = MatchRuleService()
        snapshot = job.get("rule_snapshot") or job.get("规则快照")
        if not snapshot:
            return False
        expected = job.get("rule_signature") or rule_svc.rule_signature(snapshot)
        current = rule_svc.rule_signature(rule_svc.get_entry(int(job.get("匹配规则", 0) or 0)))
        return expected == current

    def _rule_drift(self, job: dict[str, Any]) -> dict[str, Any]:
        """检查任务完成后规则是否又被更正：结果仍可追溯，但需提示审核人员。"""
        from app.services.match_rule import MatchRuleService
        rule_svc = MatchRuleService()
        snapshot = job.get("rule_snapshot") or job.get("规则快照") or {}
        expected = job.get("rule_signature") or rule_svc.rule_signature(snapshot)
        current = rule_svc.rule_signature(rule_svc.get_entry(int(job.get("匹配规则", 0) or 0)))
        snapshot_threshold = snapshot.get("相似度阈值")
        current_rule = rule_svc.get_entry(int(job.get("匹配规则", 0) or 0)) or {}
        return {
            "changed": expected != current,
            "task_rule_signature": expected,
            "current_rule_signature": current,
            "task_threshold": snapshot_threshold,
            "current_threshold": current_rule.get("相似度阈值"),
        }

    def review_cluster_manifest(self, job_id: int) -> dict[str, Any] | None:
        """给审核人员的簇名单：只返回可供审核的任务，并显式说明原因。"""
        job = self.get_entry(job_id)
        if not job:
            return None
        review_ready = job.get("review_ready")
        if review_ready is None:
            review_ready = self._review_ready(job)
        # 任务完成后规则又被更正：本轮结果仍是同一口径，但审核前必须看到提示。
        drift = self._rule_drift(job)
        if not review_ready:
            return {
                "ok": False,
                "reason": job.get("review_status")
                or "规则在计算期间已变更，本轮结果仅供追溯，不可用于审核",
                "review_ready": False,
                "job_id": job.get("任务编号"),
                "rule_signature": job.get("rule_signature"),
                "rule_drift": drift,
            }
        return {
            "ok": True,
            "review_ready": True,
            "job_id": job.get("任务编号"),
            "rule_snapshot": job.get("rule_snapshot"),
            "rule_signature": job.get("rule_signature"),
            "input_signature": job.get("input_signature"),
            "rule_drift": drift,
            "notice": (
                "任务完成后匹配规则已被更正，簇仍按任务快照生成，审核时请以 rule_snapshot 为准"
                if drift["changed"] else "规则自任务完成后未变更"
            ),
            "clusters": job.get("簇明细") or job.get("cluster_details", []),
            "registered_cluster_ids": job.get("registered_cluster_ids", []),
        }

    def aggregate_job_statistics(self, entity_model: str | None = None) -> dict[str, Any]:
        """聚合匹配任务统计：汇总已完成/失败任务数、输出簇总数和平均并行度。

        查询匹配任务存储，按实体模型分组聚合任务执行统计，
        计算已完成任务数、失败任务数、输出簇总数和质量指标。
        """
        rows = store.rows(self.MODULE)
        if entity_model:
            rows = [r for r in rows if r.get("实体模型") == entity_model]
        completed = sum(1 for r in rows if r.get("任务状态") == "已完成")
        failed = sum(1 for r in rows if r.get("任务状态") == "已失败")
        total_clusters = sum(int(r.get("输出簇数", "0")) for r in rows)
        return {
            "total_jobs": len(rows),
            "completed": completed,
            "failed": failed,
            "total_clusters_output": total_clusters,
            "success_rate": round(completed / max(len(rows), 1) * 100, 2),
        }

    def validate_job_parameters(self, job_id: int) -> dict[str, Any]:
        """校验匹配任务的参数配置是否合理：并行度、匹配策略和数据范围。"""
        job = self.get_entry(job_id)
        if not job:
            return {"ok": False, "reason": "任务不存在"}
        parallelism = int(job.get("并行度", "1"))
        checks = {
            "parallelism_valid": 1 <= parallelism <= 16,
            "has_match_rule": bool(job.get("匹配规则")),
            "has_entity_model": bool(job.get("实体模型")),
            "has_strategy": bool(job.get("匹配策略")),
        }
        checks["all_valid"] = all(checks.values())
        return {"ok": checks["all_valid"], "checks": checks}

    def retry_failed_jobs(self, entity_model: str) -> int:
        """重试已失败的匹配任务：重新调度失败任务执行模糊匹配计算。"""
        rows = store.rows(self.MODULE)
        retried = 0
        for job in rows:
            if job.get("任务状态") == "已失败" and job.get("实体模型") == entity_model:
                job["任务状态"] = "等待中"
                job["status"] = "等待中"
                job["retry_count"] = job.get("retry_count", 0) + 1
                retried += 1
        return retried
