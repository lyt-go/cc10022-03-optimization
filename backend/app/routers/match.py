"""匹配执行接口：一轮批量匹配、旧新实现对照、规则更正与审核名单。

这些接口只做参数转发，规则快照、剪枝、连通分量和审核可用性判断都在
``app.services.match_rule`` / ``app.services.auto_match_job`` 里完成。

* ``POST /api/match/round``    执行一轮匹配（不落任务），返回规则/输入快照和实做评分次数
* ``POST /api/match/compare``  同一输入分别跑旧实现与优化实现，比较成员与评分次数
* ``POST /api/match/jobs``     调度自动匹配任务并登记疑似重复簇
* ``POST /api/match/dedup``    整批去重：旧实现与优化实现对照并登记簇
* ``GET  /api/match/jobs/{id}/review`` 取审核名单，任务不可审核时明确说明原因
* ``POST /api/match/rules/{id}/parameters`` 运营人员更正规则阈值
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.auto_match_job import AutoMatchJobService
from app.services.dup_cluster import DupClusterService
from app.services.match_rule import MatchRuleService

router = APIRouter(prefix="/api/match", tags=["匹配执行"])


class MatchSource(BaseModel):
    rule_id: int
    records: list[dict[str, Any]] = Field(default_factory=list)
    entity_model: str = "customer"


class JobRequest(MatchSource):
    batch_label: str | None = None


class RuleParameterRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


@router.post("/round")
def run_round(payload: MatchSource) -> dict[str, Any]:
    result = MatchRuleService().run_match_round(payload.rule_id, payload.records)
    if result is None:
        raise HTTPException(status_code=404, detail="匹配规则不存在")
    return result


@router.post("/compare")
def compare_implementations(payload: MatchSource) -> dict[str, Any]:
    result = MatchRuleService().compare_match_implementations(payload.rule_id, payload.records)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "匹配规则不存在"))
    return result


@router.post("/jobs")
def schedule_job(payload: JobRequest) -> dict[str, Any]:
    result = AutoMatchJobService().schedule_match_job(
        payload.rule_id, payload.entity_model, payload.records,
        cluster_batch_label=payload.batch_label,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "匹配规则不存在"))
    return result


@router.post("/dedup")
def submit_batch_dedup(payload: JobRequest) -> dict[str, Any]:
    result = DupClusterService().submit_batch_dedup(
        payload.rule_id, payload.records,
        entity_model=payload.entity_model, batch_label=payload.batch_label,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "匹配规则不存在"))
    return result


@router.get("/jobs/{job_id}/review")
def review_manifest(job_id: int) -> dict[str, Any]:
    result = AutoMatchJobService().review_cluster_manifest(job_id)
    if result is None:
        raise HTTPException(status_code=404, detail="匹配任务不存在")
    return result


@router.post("/rules/{rule_id}/parameters")
def update_rule_parameters(rule_id: int, payload: RuleParameterRequest) -> dict[str, Any]:
    result = MatchRuleService().update_rule_parameters(rule_id, payload.values)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "匹配规则不存在"))
    return result
