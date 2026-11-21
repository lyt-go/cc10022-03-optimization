"""匹配规则领域服务：模糊匹配计算、相似度打分与疑似重复簇生成。

批量匹配的性能约束
------------------
* 一轮匹配只解析一次规则：规则在轮次开始时被复制成快照，整轮打分都使用
  这份快照，避免每个记录对都重新读取并解析同一条规则。
* 先算相似度上界再决定是否真正打分：每个字段同时用长度上界
  ``2*min(len_a, len_b)/(len_a+len_b)`` 与字符多重集重叠上界收紧
  ``SequenceMatcher.ratio`` 的上界，加权后仍达不到阈值的组合直接跳过，
  不再调用 SequenceMatcher。
* 取值相同的记录对只打一次分：分数只取决于字段值和规则快照。
* 上界剪枝不会改变原口径：只有“四舍五入后仍然小于阈值”的组合才会被跳过，
  保留下来的组合仍按原公式打分，再用 ``分数 >= 阈值`` 建立连边并做连通分量，
  因此临界分数与传递连边的语义都和旧实现一致。
"""
from __future__ import annotations

import copy
import datetime
import hashlib
import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Callable

from app.services.base import BaseModuleService
from app.store import store

# 参与规则指纹的字段：任一变化都会让“计算中的一轮”与当前规则不一致。
_RULE_SIGNATURE_FIELDS = (
    "id", "规则编号", "实体模型", "匹配字段", "匹配算法",
    "相似度阈值", "模糊系数", "权重分配", "规则版本", "status",
)


def _stable_hash(payload: Any) -> str:
    """对任务输入/规则生成稳定的短指纹，用于判断一轮匹配实际使用的口径。"""
    serialized = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class MatchRuleSpec:
    """一轮匹配使用的规则快照：只读、已解析，不会随运营人员的后续更正而变化。"""

    rule_id: int
    rule_code: str
    entity_model: str
    match_fields: tuple[str, ...]
    algorithm: str
    threshold: float
    fuzzy_coef: float
    weights: tuple[float, ...]
    rule_version: str
    status: str
    fingerprint: str
    prunable: bool
    raw: dict[str, Any]


class MatchRuleService(BaseModuleService):
    MODULE = "match_rule"
    ENTITY = "匹配规则"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["规则编号", "实体模型", "匹配字段", "匹配算法", "相似度阈值", "模糊系数", "权重分配", "规则版本"]
    REQUIRED_FIELDS = ["规则编号", "实体模型", "匹配字段"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已停用"]
    ACTION_RULES = {"提交校验": "待校验", "标记生效": "已生效", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    # ── 规则读取与快照 ────────────────────────────────────────────────
    @staticmethod
    def _safe_float(value: Any, default: float = 0.8) -> float:
        """安全转换浮点数，处理种子数据中的非数字值。"""
        try:
            return float(str(value))
        except (ValueError, TypeError):
            return default

    @classmethod
    def rule_signature(cls, rule: dict[str, Any] | None) -> str | None:
        """规则口径指纹：匹配字段、算法、阈值、系数、权重或版本变化都会改变它。"""
        if not rule:
            return None
        payload = {field: rule.get(field) for field in _RULE_SIGNATURE_FIELDS}
        return _stable_hash(payload)

    @classmethod
    def input_signature(cls, records: list[dict[str, Any]]) -> str:
        """实际输入指纹：记录内容或顺序变化都会改变它。"""
        return _stable_hash(records)

    def build_rule_spec(self, rule: dict[str, Any] | None) -> MatchRuleSpec | None:
        """把规则行解析成只读快照；一轮匹配只调用一次。"""
        if not rule:
            return None
        match_fields = tuple(
            field.strip() for field in str(rule.get("匹配字段", "") or "").split(",") if field.strip()
        )
        algorithm = str(rule.get("匹配算法", "fuzzy") or "fuzzy")
        threshold = self._safe_float(rule.get("相似度阈值", "0.8"))
        fuzzy_coef = self._safe_float(rule.get("模糊系数", "0.85"), 0.85)
        weight_spec = [w.strip() for w in str(rule.get("权重分配", "") or "").split(",") if w.strip()]
        weights = tuple(self._safe_float(w, 1.0) for w in weight_spec)
        # 只有能证明“上界成立”的规则才允许剪枝；任何可能放大分数的配置都退回全量打分。
        prunable = 0.0 <= fuzzy_coef <= 1.0 and all(weight >= 0 for weight in weights)
        return MatchRuleSpec(
            rule_id=int(rule.get("id", 0) or 0),
            rule_code=str(rule.get("规则编号", "") or ""),
            entity_model=str(rule.get("实体模型", "") or ""),
            match_fields=match_fields,
            algorithm=algorithm,
            threshold=threshold,
            fuzzy_coef=fuzzy_coef,
            weights=weights,
            rule_version=str(rule.get("规则版本", "") or ""),
            status=str(rule.get("status", "") or ""),
            fingerprint=self.rule_signature(rule) or "",
            prunable=prunable,
            raw=copy.deepcopy(rule),
        )

    # ── 打分 ──────────────────────────────────────────────────────────
    @staticmethod
    def _field_values(spec: MatchRuleSpec, record: dict[str, Any]) -> tuple[str, ...]:
        """一次性完成字段取值、转小写和去空白，后续打分不再重复处理。"""
        return tuple(
            str(record.get(field, "")).lower().strip() for field in spec.match_fields
        )

    @staticmethod
    def _weighted_score(spec: MatchRuleSpec, scores: list[float]) -> float:
        """按权重分配计算加权平均，保持原实现的截断和四舍五入口径。"""
        if not scores:
            return 0.0
        if spec.weights:
            weights = list(spec.weights)[:len(scores)]
        else:
            weights = [1.0] * len(scores)
        total_weight = sum(weights)
        if total_weight > 0:
            weighted = sum(s * w for s, w in zip(scores, weights)) / total_weight
        else:
            weighted = sum(scores) / len(scores)
        return min(round(weighted, 4), 1.0)

    @staticmethod
    def _char_profile(value: str) -> dict[str, int]:
        """字段值的字符计数，用于计算字符重叠上界。"""
        profile: dict[str, int] = {}
        for char in value:
            profile[char] = profile.get(char, 0) + 1
        return profile

    @staticmethod
    def _overlap(a: dict[str, int], b: dict[str, int]) -> int:
        """两个字符计数的多重集交集大小；SequenceMatcher 的匹配量不会超过它。"""
        if len(a) > len(b):
            a, b = b, a
        return sum(min(count, b[char]) for char, count in a.items() if char in b)

    @classmethod
    def _field_upper_bound(cls, spec: MatchRuleSpec, val_a: str, val_b: str,
                           profile_a: dict[str, int] | None = None,
                           profile_b: dict[str, int] | None = None) -> float:
        """单个字段相似度的严格上界，不需要真正运行 SequenceMatcher。

        ``ratio() = 2*M/T``，其中 M 是匹配字符数。匹配块由相同字符组成且互不重叠，
        因此 ``M`` 既不超过较短串长度，也不超过两串字符多重集的交集大小。
        取这两个约束后，达不到阈值的组合可以安全跳过。
        """
        if not val_a or not val_b:
            return 0.0
        if spec.algorithm in ("exact", "精确匹配"):
            return 1.0 if val_a == val_b else 0.0
        len_a, len_b = len(val_a), len(val_b)
        max_matches = min(len_a, len_b)
        if profile_a is not None and profile_b is not None:
            max_matches = min(max_matches, cls._overlap(profile_a, profile_b))
        ratio_bound = 2.0 * max_matches / (len_a + len_b)
        equal_bonus = 1.0 if val_a == val_b else 0.0
        return min(spec.fuzzy_coef * ratio_bound + (1.0 - spec.fuzzy_coef) * equal_bonus, 1.0)

    def _upper_bound_score(self, spec: MatchRuleSpec,
                           values_a: tuple[str, ...], values_b: tuple[str, ...],
                           profile_a: tuple[dict[str, int] | None, ...],
                           profile_b: tuple[dict[str, int] | None, ...]) -> float:
        """候选组合的加权上界；四舍五入后仍低于阈值即可安全剪枝。"""
        bounds = [
            self._field_upper_bound(spec, val_a, val_b, prof_a, prof_b)
            for val_a, val_b, prof_a, prof_b
            in zip(values_a, values_b, profile_a, profile_b)
        ]
        return self._weighted_score(spec, bounds)

    def _score_values(self, spec: MatchRuleSpec, values_a: tuple[str, ...],
                      values_b: tuple[str, ...],
                      cache: dict[tuple[int, str, str], float] | None = None,
                      stats: dict[str, int] | None = None) -> float:
        """按字段快照打分；字段级缓存避免对相同简称重复运行 SequenceMatcher。"""
        scores: list[float] = []
        for index, (val_a, val_b) in enumerate(zip(values_a, values_b)):
            if not val_a or not val_b:
                scores.append(0.0)
                continue
            cache_key = (index, val_a, val_b)
            if cache is not None and cache_key in cache:
                scores.append(cache[cache_key])
                continue
            if spec.algorithm in ("exact", "精确匹配"):
                score = 1.0 if val_a == val_b else 0.0
            else:
                if stats is not None:
                    stats["sequence_matcher_calls"] = stats.get("sequence_matcher_calls", 0) + 1
                similarity = SequenceMatcher(None, val_a, val_b).ratio()
                score = similarity * spec.fuzzy_coef + (1.0 - spec.fuzzy_coef) * float(val_a == val_b)
            if cache is not None:
                cache[cache_key] = score
            scores.append(score)
        return self._weighted_score(spec, scores)

    def compute_similarity(self, rule_id: int, record_a: dict[str, Any],
                            record_b: dict[str, Any], *,
                            rule: MatchRuleSpec | None = None,
                            values_a: tuple[str, ...] | None = None,
                            values_b: tuple[str, ...] | None = None,
                            cache: dict[tuple[int, str, str], float] | None = None,
                            stats: dict[str, int] | None = None) -> float:
        """计算两条记录的相似度：按匹配规则执行模糊匹配打分。

        单独调用时会读取一次规则；批量轮次传入 ``rule`` 快照和已经标准化的
        字段值，从而避免每个记录对重复解析规则、重复做字符串归一化。
        分数范围0.0-1.0，用于生成疑似重复簇。
        """
        spec = rule if isinstance(rule, MatchRuleSpec) else self.build_rule_spec(
            self.get_entry(rule_id)
        )
        if spec is None:
            return 0.0
        val_a = values_a if values_a is not None else self._field_values(spec, record_a)
        val_b = values_b if values_b is not None else self._field_values(spec, record_b)
        return self._score_values(spec, val_a, val_b, cache=cache, stats=stats)

    # ── 一轮批量匹配 ──────────────────────────────────────────────────
    def run_match_round(self, rule_id: int, records: list[dict[str, Any]], *,
                        on_pair_start: Callable[[int, int], None] | None = None,
                        on_pair_scored: Callable[[int, int, float], None] | None = None) -> dict[str, Any] | None:
        """执行一轮批量匹配，返回簇、输入/规则快照与实做评分统计。

        规则和输入都在进入循环前复制成快照。即便运营人员在计算过程中更正
        阈值，本轮仍然只使用同一份规则口径，输出不会混用两种参数；计算结束
        后再比对当前规则指纹，明确本轮结果是否可供审核。
        """
        # 在同一把锁内完成“读取规则 -> 复制快照 -> 复制输入”：
        # 若运营人员恰好在更正阈值，本轮要么看到更正前、要么看到更正后，不会读到半份配置。
        with store.lock:
            rule = self.get_entry(rule_id)
            spec = self.build_rule_spec(rule)
            if spec is None:
                return None
            # 任务数据的唯一副本：调用方后续修改 records 或规则都不会影响本轮结果。
            records_snapshot = copy.deepcopy(list(records))
        input_signature = self.input_signature(records_snapshot)
        values = [self._field_values(spec, record) for record in records_snapshot]
        # 字符计数只算一次，供上界剪枝判断使用。
        profiles = [
            tuple(self._char_profile(value) for value in record_values)
            for record_values in values
        ]

        n = len(records_snapshot)
        adjacency: dict[int, set[int]] = {i: set() for i in range(n)}
        edges: list[tuple[int, int, float]] = []
        field_cache: dict[tuple[int, str, str], float] = {}
        pair_scores: dict[tuple[tuple[str, ...], tuple[str, ...]], float] = {}
        stats = {"sequence_matcher_calls": 0}
        total_pairs = n * (n - 1) // 2
        candidate_pairs = 0
        pruned_pairs = 0
        scored_pairs = 0
        cache_hits = 0

        for i in range(n):
            values_i = values[i]
            profile_i = profiles[i]
            for j in range(i + 1, n):
                # 回调只用于演示“运营人员在一轮计算中途更正阈值”，不参与打分。
                if on_pair_start is not None:
                    on_pair_start(i, j)
                # 先用上界判断：达不到阈值就不做真正的相似度计算。
                if spec.prunable:
                    upper_bound = self._upper_bound_score(
                        spec, values_i, values[j], profile_i, profiles[j]
                    )
                    if upper_bound < spec.threshold:
                        pruned_pairs += 1
                        continue
                candidate_pairs += 1
                # 取值完全相同的记录对只需打一次分：分数只取决于字段值和规则快照。
                value_pair = (values_i, values[j])
                if value_pair in pair_scores:
                    similarity = pair_scores[value_pair]
                    cache_hits += 1
                else:
                    similarity = self.compute_similarity(
                        rule_id,
                        records_snapshot[i],
                        records_snapshot[j],
                        rule=spec,
                        values_a=values_i,
                        values_b=values[j],
                        cache=field_cache,
                        stats=stats,
                    )
                    pair_scores[value_pair] = similarity
                    scored_pairs += 1
                if on_pair_scored is not None:
                    on_pair_scored(i, j, similarity)
                # 原口径：临界分数（等于阈值）也算达标。
                if similarity >= spec.threshold:
                    adjacency[i].add(j)
                    adjacency[j].add(i)
                    edges.append((i, j, similarity))

        # 原口径：按全部达标连边做连通分量，允许经第三条记录传递连通。
        visited: set[int] = set()
        clusters: list[list[int]] = []
        for start in range(n):
            if start in visited:
                continue
            stack = [start]
            component: list[int] = []
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                stack.extend(adjacency[node] - visited)
            if len(component) > 1:
                clusters.append(sorted(component))

        cluster_details = self._build_cluster_details(
            spec, records_snapshot, clusters, edges, input_signature
        )

        with store.lock:
            current_signature = self.rule_signature(self.get_entry(rule_id))
        rule_changed = current_signature != spec.fingerprint
        review_ready = not rule_changed

        return {
            "ok": True,
            "rule_id": rule_id,
            "rule_code": spec.rule_code,
            "rule_version": spec.rule_version,
            "rule_snapshot": copy.deepcopy(spec.raw),
            "规则快照": copy.deepcopy(spec.raw),
            "rule_signature": spec.fingerprint,
            "规则签名": spec.fingerprint,
            "current_rule_signature": current_signature,
            "rule_changed": rule_changed,
            "input_count": n,
            "input_signature": input_signature,
            "输入签名": input_signature,
            "输入快照": {
                "记录数": n,
                "签名": input_signature,
                "匹配字段": list(spec.match_fields),
                "记录": copy.deepcopy(records_snapshot),
            },
            "threshold": spec.threshold,
            "match_fields": list(spec.match_fields),
            "clusters": clusters,
            "cluster_count": len(clusters),
            "cluster_details": cluster_details,
            "簇明细": cluster_details,
            "total_pairs": total_pairs,
            "candidate_pairs": candidate_pairs,
            "pruned_pairs": pruned_pairs,
            "scored_pairs": scored_pairs,
            "sequence_matcher_calls": stats["sequence_matcher_calls"],
            "pair_cache_hits": cache_hits,
            "rule_lookups": 1,
            "total_comparisons": total_pairs,
            "实做评分次数": scored_pairs,
            "总组合数": total_pairs,
            "候选组合数": candidate_pairs,
            "剪枝组合数": pruned_pairs,
            "序列比较次数": stats["sequence_matcher_calls"],
            "相同取值省去的评分次数": cache_hits,
            "规则解析次数": 1,
            "review_ready": review_ready,
            "可供审核": review_ready,
            "review_status": "可供审核" if review_ready else "规则在计算期间已变更，本轮结果仅供追溯，不可用于审核",
            "审核结论": "可供审核" if review_ready else "规则在计算期间已变更，本轮结果仅供追溯，不可用于审核",
            "computed_at": datetime.datetime.utcnow().isoformat(),
        }

    def _build_cluster_details(self, spec: MatchRuleSpec,
                               records: list[dict[str, Any]],
                               clusters: list[list[int]],
                               edges: list[tuple[int, int, float]],
                               input_signature: str) -> list[dict[str, Any]]:
        """把簇索引展开成审核人员能直接看的成员与相似度摘要。"""
        details: list[dict[str, Any]] = []
        for index, cluster in enumerate(clusters):
            cluster_set = set(cluster)
            similarities = [
                similarity for left, right, similarity in edges
                if left in cluster_set and right in cluster_set
            ]
            members = []
            for record_index in cluster:
                record = records[record_index] if record_index < len(records) else {}
                members.append({
                    "索引": record_index,
                    "字段值": {field: record.get(field) for field in spec.match_fields},
                    "记录": copy.deepcopy(record),
                })
            details.append({
                "簇序号": index,
                "成员索引": list(cluster),
                "成员": members,
                "簇大小": len(cluster),
                "相似度均值": round(sum(similarities) / len(similarities), 4) if similarities else 0.0,
                "达标连边数": len(similarities),
                "输入签名": input_signature,
                "规则签名": spec.fingerprint,
            })
        return details

    def generate_duplicate_clusters(self, rule_id: int,
                                     records: list[dict[str, Any]]) -> list[list[int]]:
        """生成疑似重复簇：一轮批量匹配后返回每条簇的记录下标。

        与旧实现相比，这里只解析一次规则，并用相似度上界跳过明显达不到
        阈值的组合；簇成员、临界分数和传递连边口径保持不变。
        """
        result = self.run_match_round(rule_id, records)
        if result is None:
            return []
        return result["clusters"]

    # ── 旧实现：仅用于口径对照与性能对比 ──────────────────────────────
    def _legacy_run(self, rule_id: int, records: list[dict[str, Any]],
                    spec: MatchRuleSpec | None = None) -> tuple[list[list[int]], int, int]:
        """逐字保留旧版两两打分逻辑，返回簇、实做评分次数和规则解析次数。

        传入 ``spec`` 时使用同一份规则快照，保证对照实验两侧口径完全相同；
        不传时保持旧行为——每个记录对重新读取并解析一次规则。
        """
        rule = spec.raw if spec is not None else self.get_entry(rule_id)
        if not rule:
            return [], 0, 0
        threshold = spec.threshold if spec is not None else self._safe_float(rule.get("相似度阈值", "0.8"))
        n = len(records)
        adjacency: dict[int, set[int]] = {i: set() for i in range(n)}
        scoring_calls = 0
        for i in range(n):
            for j in range(i + 1, n):
                similarity = self.compute_similarity(
                    rule_id, records[i], records[j], rule=spec
                )
                scoring_calls += 1
                if similarity >= threshold:
                    adjacency[i].add(j)
                    adjacency[j].add(i)
        visited: set[int] = set()
        clusters: list[list[int]] = []
        for i in range(n):
            if i in visited:
                continue
            stack = [i]
            component: list[int] = []
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                component.append(node)
                stack.extend(adjacency[node] - visited)
            if len(component) > 1:
                clusters.append(sorted(component))
        return clusters, scoring_calls, scoring_calls

    def generate_duplicate_clusters_legacy(self, rule_id: int,
                                            records: list[dict[str, Any]]) -> list[list[int]]:
        """旧版簇生成入口，保留用于回归对照；生产批量匹配请使用优化入口。"""
        clusters, _, _ = self._legacy_run(rule_id, records)
        return clusters

    def compare_match_implementations(self, rule_id: int,
                                      records: list[dict[str, Any]]) -> dict[str, Any]:
        """对同一输入分别运行旧实现和优化实现，比较成员与实做评分次数。"""
        spec = self.build_rule_spec(self.get_entry(rule_id))
        if spec is None:
            return {"ok": False, "reason": "匹配规则不存在"}
        # 两侧共用同一份规则快照，避免对照期间规则被更正导致口径不一致。
        legacy_clusters, legacy_calls, legacy_lookups = self._legacy_run(rule_id, records, spec)
        optimized = self.run_match_round(rule_id, records)
        if optimized is None:
            return {
                "ok": False,
                "reason": "匹配规则不存在",
                "legacy_clusters": legacy_clusters,
                "legacy_scoring_calls": legacy_calls,
            }
        return {
            "ok": True,
            "legacy_clusters": legacy_clusters,
            "optimized_clusters": optimized["clusters"],
            "clusters_equal": legacy_clusters == optimized["clusters"],
            "legacy_members": [list(cluster) for cluster in legacy_clusters],
            "optimized_members": [list(cluster) for cluster in optimized["clusters"]],
            "legacy_scoring_calls": legacy_calls,
            "optimized_scoring_calls": optimized["scored_pairs"],
            "optimized_sequence_matcher_calls": optimized["sequence_matcher_calls"],
            "scoring_calls_saved": legacy_calls - optimized["scored_pairs"],
            "legacy_rule_lookups": legacy_lookups,
            "optimized_rule_lookups": optimized["rule_lookups"],
            "optimized": optimized,
        }

    # ── 规则维护 ──────────────────────────────────────────────────────
    _PARAMETER_ALIASES = {
        "threshold": "相似度阈值",
        "fuzzy_coef": "模糊系数",
        "match_fields": "匹配字段",
        "algorithm": "匹配算法",
        "weights": "权重分配",
        "version": "规则版本",
        "status": "status",
    }

    def update_rule_parameters(self, rule_id: int,
                               values: dict[str, Any] | None = None,
                               **changes: Any) -> dict[str, Any]:
        """运营人员更正规则参数（如阈值），返回新的规则口径指纹。

        运行中的匹配轮次持有的是开始时的规则快照，不会读取这里的改动；
        轮次结束时会发现指纹变化并把结果标记为不可审核。
        """
        with store.lock:
            rule = self.get_entry(rule_id)
            if not rule:
                return {"ok": False, "reason": "规则不存在"}
            previous_signature = self.rule_signature(rule)
            updates: dict[str, Any] = {}
            if values:
                updates.update(values)
            updates.update(changes)
            for key, value in updates.items():
                target = self._PARAMETER_ALIASES.get(key, key)
                if target in _RULE_SIGNATURE_FIELDS:
                    rule[target] = value
            return {
                "ok": True,
                "rule": rule,
                "signature": self.rule_signature(rule),
                "previous_signature": previous_signature,
            }

    def update_rule_threshold(self, rule_id: int, threshold: Any) -> dict[str, Any]:
        """更正规则相似度阈值，供运营人员在任务运行期间调整口径。"""
        return self.update_rule_parameters(rule_id, {"相似度阈值": threshold})

    # ── 其他 ──────────────────────────────────────────────────────────
    def validate_match_rule_parameters(self, rule_id: int) -> dict[str, Any]:
        """校验匹配规则参数的有效性：阈值、系数和权重分配范围检查。

        读取匹配规则配置，校验相似度阈值范围(0-1)、模糊系数范围、
        权重分配是否总和约为1.0，输出参数校验结果。
        """
        rule = self.get_entry(rule_id)
        if not rule:
            return {"ok": False, "reason": "规则不存在"}
        threshold = self._safe_float(rule.get("相似度阈值", "0.8"))
        fuzzy_coef = self._safe_float(rule.get("模糊系数", "0.85"), 0.85)
        checks = {
            "threshold_valid": 0 < threshold <= 1,
            "fuzzy_coef_valid": 0 < fuzzy_coef <= 1,
            "threshold": threshold,
            "fuzzy_coef": fuzzy_coef,
        }
        checks["all_valid"] = all([checks["threshold_valid"], checks["fuzzy_coef_valid"]])
        return {"ok": checks["all_valid"], "checks": checks}

    def query_active_match_rules(self, entity_model: str) -> list[dict[str, Any]]:
        """查询指定实体模型的所有已生效匹配规则。

        遍历匹配规则存储，筛选已生效且匹配实体模型的规则，
        按规则版本排序返回供匹配任务调度使用。
        """
        rows = store.rows(self.MODULE)
        rules = [
            row for row in rows
            if row.get("实体模型") == entity_model and row.get("status") == "已生效"
        ]
        # 查询匹配规则并按规则版本排序
        rules.sort(key=lambda r: r.get("规则版本", ""), reverse=True)
        return rules
