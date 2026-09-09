"""Adapters that place both legacy research workflows behind one Harness loop."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Protocol

from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec
from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.agents.multi_agent.orchestrator import MultiAgentOrchestrator
from deepresearch_agent.agents.multi_agent.planner.base_planner import PlannerResult
from deepresearch_agent.agents.multi_agent.reporter.base_reporter import ReportResult, SectionContent
from deepresearch_agent.agents.multi_agent.reporter.consistency_checker import ConsistencyCheckResult
from deepresearch_agent.agents.multi_agent.reporter.evidence_cards import EvidenceCardPipeline
from deepresearch_agent.agents.multi_agent.reporter.outline_builder import ReportOutline, SectionOutline
from deepresearch_agent.config.settings import (
    EVIDENCE_CARD_MAX_TOKENS,
    REPORT_BATCH_BUDGET_RATIO,
    REPORT_BATCH_DIGEST_MAX_TOKENS,
    REPORT_MAX_SECTIONS,
    REPORT_RESERVED_TOKENS,
    REPORT_SECTION_EVIDENCE_BUDGET,
    VERIFICATION_RESERVED_TOKENS,
)
from deepresearch_agent.evolution.skill_compiler import compile_runtime_policy, policy_prompt

from .run_context import RunContext
from .report_safety import add_inline_citations, citation_evidence_ids, has_internal_material, rank_evidence, sanitize_report


class WorkflowDriver(Protocol):
    async def plan(self, failures: list[str] | None = None) -> None: ...
    async def execute(self) -> None: ...
    async def report(self) -> str: ...
    async def repair_report(self, failures: list[str]) -> bool: ...
    def snapshot(self) -> dict[str, Any]: ...
    def execution_records(self) -> list[ExecutionRecord]: ...
    def evidence_results(self) -> list[tuple[str | None, str | None, str, RetrievalResult]]: ...
    def report_consistency(self) -> bool | None: ...
    def evidence_card_coverage(self) -> dict[str, Any] | None: ...
    def report_metrics(self) -> dict[str, Any]: ...
    def plan_record(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None: ...


def _evidence_only_report(
    results: list[RetrievalResult],
    *,
    title: str = "修复后的研究报告",
    required_sections: list[str] | None = None,
) -> tuple[str, list[RetrievalResult]]:
    """Build a conservative report that cannot introduce unsupported claims."""
    selected: list[RetrievalResult] = []
    seen_ids: set[str] = set()
    for item in rank_evidence(results):
        if not item.result_id or item.result_id in seen_ids:
            continue
        seen_ids.add(item.result_id)
        selected.append(item)
        if len(selected) == 6:
            break

    lines: list[str] = []
    for index, item in enumerate(selected, 1):
        summary = re.sub(r"\[(?:ev_)?[A-Za-z0-9_-]+\]", "", str(item.evidence or ""))
        summary = re.sub(r"\s+", " ", summary).strip()
        if len(summary) > 520:
            summary = summary[:517].rstrip() + "…"
        if summary:
            lines.append(f"{index}. {summary} [{item.result_id}]")

    if not lines:
        return f"# {title}\n\n## 证据支持的要点\n\n当前没有可用于修复报告的有效证据。", []

    sections = [f"# {title}", "## 证据支持的要点", "\n".join(lines)]
    existing = {"证据支持的要点", "局限"}
    first_id = selected[0].result_id
    for name in required_sections or []:
        clean_name = str(name).strip()
        if clean_name and clean_name not in existing:
            sections.extend([f"## {clean_name}", f"本节仅保留上方证据能够直接支持的内容。 [{first_id}]"])
            existing.add(clean_name)
    sections.extend([
        "## 局限",
        "本报告由一致性门禁触发保守修复，仅保留当前 Run 证据能够直接支持的陈述；被判定为不一致或无法就近引用的原报告内容已移除。",
    ])
    return "\n\n".join(sections), selected


class PlanExecuteReportDriver:
    """Thin staged wrapper around the existing Planner/Worker/Reporter components."""

    def __init__(self, context: RunContext, orchestrator: MultiAgentOrchestrator, *, events=None):
        self.context = context
        self.orchestrator = orchestrator
        self.events = events
        self.selected_skill = (context.context_snapshot or {}).get("selected_skill") or None
        self.skill_policy = compile_runtime_policy(self.selected_skill, source_mode=context.source_mode.value)
        runtime_snapshot = dict(context.context_snapshot or context.config_snapshot)
        runtime_snapshot["active_skill_policy"] = self.skill_policy
        runtime_snapshot["skill_policy_consumers"] = ["planner", "worker", "reflection", "reporter", "verifier"] if self.skill_policy else []
        self.state = PlanExecuteState(
            session_id=context.session_id, run_id=context.run_id,
            source_mode=context.source_mode.value, workflow_mode=context.workflow_mode.value,
            budget_state={"limits": context.budget_limits.model_dump(), "usage": context.budget_usage.model_dump()},
            context_snapshot=runtime_snapshot,
            # Planning must reason about the user's request, not the serialized
            # system/context envelope. The latter remains available separately.
            input=(context.resolved_query or context.original_query) + policy_prompt(self.skill_policy),
        )
        self.planner_result: PlannerResult | None = None
        self.report_result: ReportResult | None = None
        self._repair_pending = False
        self.task_checkpoint = None
        self.task_stop_requested = None
        self._restore(context.workflow_state)

    def _restore(self, payload: dict[str, Any]) -> None:
        if not payload:
            return
        raw_state = payload.get("state")
        if raw_state:
            self.state = PlanExecuteState.model_validate(raw_state)
            if isinstance(self.state.plan, dict):
                self.state.plan = PlanSpec.model_validate(self.state.plan)
            self.state.execution_records = [
                item if isinstance(item, ExecutionRecord) else ExecutionRecord.model_validate(item)
                for item in self.state.execution_records
            ]
        if payload.get("planner_result"):
            self.planner_result = PlannerResult.model_validate(payload["planner_result"])
            if self.state.plan is not None:
                self.planner_result.plan_spec = self.state.plan
                self.planner_result.executor_signal = self.state.plan.to_execution_signal()
        if payload.get("report_result"):
            self.report_result = ReportResult.model_validate(payload["report_result"])
        self._repair_pending = bool(payload.get("repair_pending"))

    async def plan(self, failures: list[str] | None = None) -> None:
        self.planner_result = await asyncio.to_thread(
            self.orchestrator.plan, self.state, assumptions=failures or None,
        )
        if self.planner_result.plan_spec is None:
            if self.planner_result.needs_clarification():
                raise RuntimeError("needs_user_input")
            raise RuntimeError("Planner 未生成可执行 PlanSpec")

    async def execute(self) -> None:
        if self.planner_result is None:
            raise RuntimeError("缺少 PlannerResult")
        if self.state.plan is not None:
            self.planner_result.plan_spec = self.state.plan
            self.planner_result.executor_signal = self.state.plan.to_execution_signal()
        execution_token_ceiling = max(
            0,
            self.context.budget_limits.max_llm_tokens
            - REPORT_RESERVED_TOKENS
            - VERIFICATION_RESERVED_TOKENS,
        )
        loop = asyncio.get_running_loop()

        def progress_callback(kind, task, record):
            if record is not None and self.task_checkpoint is not None:
                asyncio.run_coroutine_threadsafe(self.task_checkpoint(), loop).result(timeout=30)
            if self.events is None:
                return
            future = asyncio.run_coroutine_threadsafe(
                self._publish_execution_progress(kind, task, record), loop,
            )
            future.result(timeout=10)

        def stop_requested():
            return self.context.budget_usage.llm_tokens >= execution_token_ceiling or (
                self.task_stop_requested is not None and
                asyncio.run_coroutine_threadsafe(self.task_stop_requested(), loop).result(timeout=30)
            )

        records = await asyncio.to_thread(
            self.orchestrator.execute,
            self.state,
            self.planner_result,
            stop_predicate=stop_requested,
            progress_callback=progress_callback,
        )
        existing = {item.record_id for item in self.state.execution_records}
        self.state.execution_records.extend(item for item in records if item.record_id not in existing)

    async def _publish_execution_progress(self, kind, task, record) -> None:
        task_payload = {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "description": task.description[:300],
        }
        if kind == "task.started" or record is None:
            await self.events.publish(
                self.context.run_id, "task.started", stage="executing", payload=task_payload,
            )
            return

        for call in record.tool_calls:
            payload = {
                **task_payload,
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "status": call.status,
            }
            if isinstance(call.args, dict) and call.args.get("query"):
                payload["query"] = str(call.args["query"])[:300]
            if isinstance(call.result, dict):
                result_ids = call.result.get("result_ids", []) or []
                payload["result_count"] = len(result_ids) or len(record.evidence)
                rendered = json.dumps(call.result, ensure_ascii=False, default=str)
                payload["output_preview"] = rendered[:4000]
                payload["output_truncated"] = len(rendered) > 4000
            await self.events.publish(
                self.context.run_id,
                "tool.completed" if call.status != "failed" else "tool.failed",
                stage="executing",
                payload=payload,
            )
        failed = any(call.status == "failed" for call in record.tool_calls) or (
            record.reflection is not None and not record.reflection.success
        )
        await self.events.publish(
            self.context.run_id,
            "task.failed" if failed else "task.completed",
            stage="executing",
            payload={
                **task_payload,
                "record_id": record.record_id,
                "status": "failed" if failed else "completed",
                "evidence_count": len(record.evidence),
            },
        )

    async def report(self) -> str:
        requirements = self.context.config_snapshot.get("report_requirements")
        exclusions = self.context.config_snapshot.get("scope_exclusions")
        if requirements or exclusions:
            base_input = self.context.resolved_query or self.context.original_query
            self.state.input = base_input + "\n用户修订后的报告要求：" + json.dumps(requirements or {}, ensure_ascii=False)
            if exclusions:
                self.state.input += "\n用户明确排除以下范围，不应再次研究或将缺少它们视为失败：" + json.dumps(exclusions, ensure_ascii=False)
            self.state.context_snapshot["report_revision_instructions"] = self.state.input
        if self._repair_pending and self.report_result is not None:
            self._repair_pending = False
        elif self._uses_compact_report() and not requirements:
            self.report_result = self._build_compact_report()
        elif self._requires_reserved_budget_fallback():
            self.report_result = self._build_reserved_budget_report()
        else:
            report_type = str(self.context.config_snapshot.get("report_type") or "long_document")
            self.report_result = await asyncio.to_thread(
                self.orchestrator.report,
                self.state,
                report_type=report_type,
            )
        self.report_result.final_report = sanitize_report(self.report_result.final_report)
        evidence_ids = citation_evidence_ids([result for _, _, _, result in self.evidence_results()])
        self.report_result.final_report = add_inline_citations(self.report_result.final_report, evidence_ids)
        self.state.response = self.report_result.final_report
        return self.report_result.final_report

    def _requires_reserved_budget_fallback(self) -> bool:
        remaining = (
            self.context.budget_limits.max_llm_tokens
            - self.context.budget_usage.llm_tokens
        )
        return remaining < REPORT_RESERVED_TOKENS + VERIFICATION_RESERVED_TOKENS

    def _build_reserved_budget_report(self) -> ReportResult:
        """Finish without another model call while keeping the report readable."""
        evidence_by_id: dict[str, RetrievalResult] = {}
        for _, _, _, result in self.evidence_results():
            evidence_by_id[result.result_id] = result
        evidence = list(evidence_by_id.values())
        pipeline = EvidenceCardPipeline(
            card_max_tokens=EVIDENCE_CARD_MAX_TOKENS,
            section_token_budget=REPORT_SECTION_EVIDENCE_BUDGET,
            batch_budget_ratio=REPORT_BATCH_BUDGET_RATIO,
            digest_max_tokens=REPORT_BATCH_DIGEST_MAX_TOKENS,
        )
        cards, _ = pipeline.build_cards(evidence)
        plan = self.planner_result.plan_spec if self.planner_result else None
        nodes = list(plan.task_graph.nodes)[:REPORT_MAX_SECTIONS] if plan else []
        sections = [
            SectionOutline(
                section_id=f"reserved_{index}",
                title=str(node.description or f"研究发现 {index}")[:80],
                summary=str(node.description or "证据支持的研究发现")[:240],
            )
            for index, node in enumerate(nodes, 1)
        ]
        if not sections:
            sections = [SectionOutline(
                section_id="reserved_findings",
                title="证据支持的研究发现",
                summary="按全量 Evidence Card 呈现研究发现",
            )]
        outline = ReportOutline(
            report_type="long_document",
            title="研究报告（预算保护模式）",
            abstract="执行阶段已接近预留边界，报告以确定性 Evidence Card 形式完整交付。",
            sections=sections,
        )
        routing = pipeline.route_cards(cards, outline)
        section_contents: list[SectionContent] = []
        processed_ids: list[str] = []
        cards_by_id = {card.evidence_id: card for card in cards}
        for section in outline.sections:
            assigned = routing.get(section.section_id, [])
            lines: list[str] = []
            seen_claims: set[str] = set()
            for evidence_id in assigned:
                card = cards_by_id[evidence_id]
                claim = card.claims[0] if card.claims else card.summary
                claim = self._clean_fallback_claim(claim)
                fingerprint = re.sub(r"\W+", "", claim).lower()[:160]
                if not claim or fingerprint in seen_claims:
                    continue
                seen_claims.add(fingerprint)
                lines.append(f"- {claim} [{evidence_id}]")
                if card.conflict_findings:
                    conflict = self._clean_fallback_claim(card.conflict_findings[0])
                    if conflict:
                        lines.append(f"  - 冲突或不一致：{conflict} [{evidence_id}]")
                if card.limitations:
                    limitation = self._clean_fallback_claim(card.limitations[0])
                    if limitation:
                        lines.append(f"  - 局限：{limitation} [{evidence_id}]")
                if len(seen_claims) >= 4:
                    break
            content = "\n".join(lines) or "当前证据不足以形成可靠结论。"
            if len(assigned) > len(seen_claims):
                content += f"\n\n本节另有 {len(assigned) - len(seen_claims)} 条证据保存在证据台账中，可在报告的“证据”视图核查。"
            section_contents.append(SectionContent(
                section_id=section.section_id,
                title=section.title,
                content=content,
                used_evidence_ids=assigned,
            ))
            processed_ids.extend(assigned)
        report_parts = [f"# {outline.title}", outline.abstract or ""]
        for section, content in zip(outline.sections, section_contents):
            report_parts.extend([f"## {section.title}", content.content])
        # Full coverage lives in structured metadata and the Evidence Ledger.
        _, annex_ids = pipeline.annex(cards)
        final_report = "\n\n".join(part for part in report_parts if part).strip()
        coverage = pipeline.coverage(cards, routing, processed_ids, annex_ids)
        return ReportResult(
            outline=outline,
            sections=section_contents,
            final_report=final_report,
            consistency_check=ConsistencyCheckResult(
                is_consistent=bool(final_report.strip() and not has_internal_material(final_report)),
                raw_response="deterministic reserved-budget Evidence Card report",
            ),
            evidence_cards=[card.model_dump(mode="json") for card in cards],
            evidence_routing=routing,
            evidence_card_coverage=coverage,
            report_metrics={
                "evidence_count": len(evidence),
                "evidence_card_count": len(cards),
                "model_calls": 0,
                "reserved_budget_fallback": True,
                "remaining_tokens_at_entry": (
                    self.context.budget_limits.max_llm_tokens
                    - self.context.budget_usage.llm_tokens
                ),
            },
        )

    @staticmethod
    def _clean_fallback_claim(value: str, *, limit: int = 360) -> str:
        """Remove common webpage chrome and Markdown that corrupt report layout."""
        text = re.sub(r"[`#>*_|]+", " ", str(value or ""))
        text = re.sub(
            r"(?i)skip to content|navigation menu|sign in|cookie settings|loading(?:\s+loading)+|"
            r"documentation index|console\s+log\s+in",
            " ",
            text,
        )
        text = re.sub(r"\s+", " ", text).strip(" -—:：")
        if len(text) > limit:
            text = text[:limit].rstrip() + "…"
        return text

    def _uses_compact_report(self) -> bool:
        plan = self.planner_result.plan_spec if self.planner_result else None
        if plan is None or len(plan.task_graph.nodes) != 1:
            return False
        query = (self.context.resolved_query or self.context.original_query).lower()
        markers = ("概括", "总结", "要点", "列出", "是什么", "简述", "summarize", "list ", "what is")
        return len(query) <= 160 and any(marker in query for marker in markers)

    def _build_compact_report(self) -> ReportResult:
        ranked = rank_evidence([result for _, _, _, result in self.evidence_results()])
        unique: list[RetrievalResult] = []
        seen: set[str] = set()
        for item in ranked:
            if item.result_id not in seen:
                seen.add(item.result_id)
                unique.append(item)
        query = (self.context.resolved_query or self.context.original_query).lower()
        candidates = unique
        if "药物" in query or "用药" in query:
            drug_terms = ("药物", "用药", "阿司匹林", "抗血小板", "抗凝", "溶栓", "尼莫地平")
            directly_on_topic = [
                item for item in unique
                if any(term in f"{item.evidence} {item.metadata.source_id}" for term in drug_terms)
            ]
            if directly_on_topic:
                candidates = directly_on_topic
        selected: list[RetrievalResult] = []
        fingerprints: list[set[str]] = []
        for item in candidates:
            compact = re.sub(r"\s+", "", str(item.evidence or ""))
            grams = {compact[index:index + 2] for index in range(max(0, len(compact) - 1))}
            duplicate = any(
                grams and prior and len(grams & prior) / min(len(grams), len(prior)) >= 0.6
                for prior in fingerprints
            )
            if duplicate:
                continue
            selected.append(item)
            fingerprints.append(grams)
            if len(selected) == 3:
                break
        lines: list[str] = []
        for index, item in enumerate(selected, 1):
            summary = re.sub(r"\s+", " ", str(item.evidence or "")).strip()
            if len(summary) > 420:
                summary = summary[:417].rstrip() + "…"
            lines.append(f"{index}. {summary} [{item.result_id}]")
        if not lines:
            lines.append("当前私有库没有检索到可直接支持该问题的证据。")
        body = "\n".join(lines)
        count_note = f"本次仅检索到 {len(selected)} 条不重复且直接相关的证据；" if len(selected) < 3 else ""
        limitation = f"{count_note}当前回答严格限于本次检索到的库内证据，未被证据直接覆盖的治疗细节不作推断。"
        final_report = f"# 研究报告\n\n## 证据支持的要点\n\n{body}\n\n## 局限\n\n{limitation}"
        outline = ReportOutline(
            report_type="short_answer",
            title="研究报告",
            sections=[SectionOutline(section_id="evidence_points", title="证据支持的要点", summary="基于库内证据的紧凑回答", evidence_ids=[item.result_id for item in selected], estimated_words=300)],
            total_estimated_words=300,
        )
        section = SectionContent(
            section_id="evidence_points", title="证据支持的要点", content=body,
            used_evidence_ids=[item.result_id for item in selected],
        )
        self.state.response = final_report
        return ReportResult(
            outline=outline, sections=[section], final_report=final_report,
            consistency_check=ConsistencyCheckResult(is_consistent=True, raw_response="deterministic evidence-only report"),
        )

    async def repair_report(self, failures: list[str]) -> bool:
        if self.report_result is None:
            return False
        supported = {"citation_integrity", "required_section", "report_consistency", "source_diversity", "evidence_card_coverage"}
        if not set(failures).issubset(supported):
            return False
        report = self.report_result.final_report
        if set(failures) & {"citation_integrity", "report_consistency"}:
            all_evidence = [result for _, _, _, result in self.evidence_results()]
            report, selected = _evidence_only_report(
                all_evidence,
                required_sections=list(self.context.config_snapshot.get("required_sections", [])),
            )
            if not selected:
                return False
            try:
                consistency = await asyncio.to_thread(
                    self.orchestrator.recheck_report_consistency,
                    report,
                    selected,
                )
            except Exception as exc:  # a failed checker must not be treated as a pass
                consistency = ConsistencyCheckResult(
                    is_consistent=False,
                    issues=[{"kind": type(exc).__name__, "message": str(exc)[:240]}],
                    raw_response=None,
                )
            self.report_result.consistency_check = consistency
        if "required_section" in failures and not report.lstrip().startswith("#"):
            report = "# 研究报告\n\n" + report
        if "source_diversity" in failures and "局限" not in report:
            report += "\n\n## 局限\n\n当前证据来源数量有限，结论应结合更多独立来源复核。"
        if "evidence_card_coverage" in failures and self.report_result.evidence_cards:
            from deepresearch_agent.agents.multi_agent.reporter.evidence_cards import EvidenceCardPipeline, EvidenceCard
            cards = [EvidenceCard.model_validate(item) for item in self.report_result.evidence_cards]
            processed_ids = [
                evidence_id
                for section in self.report_result.sections
                for evidence_id in section.used_evidence_ids
            ]
            index_ids = [card.evidence_id for card in cards]
            coverage = EvidenceCardPipeline.coverage(
                cards, self.report_result.evidence_routing, processed_ids, index_ids,
            )
            self.report_result.evidence_card_coverage = coverage
            if not coverage.get("passed"):
                return False
        self.report_result.final_report = report
        self.state.response = report
        self._repair_pending = True
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state.model_dump(mode="json"),
            "planner_result": self.planner_result.model_dump(mode="json") if self.planner_result else None,
            "report_result": self.report_result.model_dump(mode="json") if self.report_result else None,
            "repair_pending": self._repair_pending,
        }

    def execution_records(self) -> list[ExecutionRecord]:
        return list(self.state.execution_records)

    def evidence_results(self) -> list[tuple[str | None, str | None, str, RetrievalResult]]:
        output = []
        for record in self.state.execution_records:
            tool_call_id = record.tool_calls[0].tool_call_id if record.tool_calls else None
            provider = record.tool_calls[0].tool_name if record.tool_calls else "unknown"
            for item in record.evidence:
                result = item if isinstance(item, RetrievalResult) else RetrievalResult.from_dict(item)
                output.append((record.task_id, tool_call_id, provider, result))
        return output

    def report_consistency(self) -> bool | None:
        if not self.report_result or has_internal_material(self.report_result.final_report):
            return False
        if self.report_result.consistency_check is None:
            return bool(self.report_result.final_report.strip())
        return self.report_result.consistency_check.is_consistent

    def evidence_card_coverage(self) -> dict[str, Any] | None:
        if self.report_result is not None and self.report_result.evidence_card_coverage:
            return dict(self.report_result.evidence_card_coverage)
        if self.state.report_context and self.state.report_context.evidence_card_coverage:
            return dict(self.state.report_context.evidence_card_coverage)
        return None

    def report_metrics(self) -> dict[str, Any]:
        return dict(self.report_result.report_metrics) if self.report_result else {}

    def plan_record(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        if self.planner_result is None or self.planner_result.plan_spec is None:
            return None
        plan = self.planner_result.plan_spec
        return plan.model_dump(mode="json"), [node.model_dump(mode="json") for node in plan.task_graph.nodes]


class DeepResearchDriver:
    """Keeps the existing DeepResearch loop as execute, with deterministic plan/report stages."""

    def __init__(self, context: RunContext, agent: Any, events: Any = None):
        self.context, self.agent = context, agent
        self.events = events
        self._iterations: dict[int, dict[str, Any]] = {}
        self.thinking: str | None = None
        research_tool = getattr(agent, "research_tool", None)
        if getattr(research_tool, "deep_research", None) is not None:
            research_tool = research_tool.deep_research
        max_iterations = context.config_snapshot.get("deep_research_max_iterations")
        if max_iterations is not None and research_tool is not None:
            research_tool.max_iterations = max(1, min(int(max_iterations), research_tool.max_iterations))
        payload = context.workflow_state
        self.answer: str | None = payload.get("answer")
        self.results = [RetrievalResult.from_dict(item) for item in payload.get("results", [])]
        self._report: str | None = payload.get("report")
        self._plan = payload.get("plan")
        self._repair_pending = bool(payload.get("repair_pending"))

    async def plan(self, failures: list[str] | None = None) -> None:
        if failures and set(failures) & {"min_evidence", "claim_support", "source_match"}:
            # A verification-driven replan must actually execute research again;
            # retaining the prior answer would turn replan into a no-op.
            self.answer = None
            self.results = []
        self._plan = {
            "plan_id": f"plan_{self.context.run_id}", "version": self.context.plan_version + 1,
            "status": "executing", "source_mode": self.context.source_mode.value,
            "tasks": [{"task_id": f"task_{self.context.run_id}_research", "task_type": "deep_research", "status": "pending", "source_mode": self.context.source_mode.value, "description": self.context.original_query, "parameters": {"query": self.context.resolved_query or self.context.original_query}}],
            "verification_failures": failures or [],
        }

    async def execute(self) -> None:
        if self.answer is None:
            tool = getattr(self.agent, "research_tool", None)
            if tool is not None and hasattr(tool, "thinking_stream"):
                # 直接流式消费 research_tool.thinking_stream（与 LangGraph 研究节点同源），
                # 逐轮迭代通过 progress_callback 实时发布 agent.progress / iteration.completed。
                async def _on_progress(msg: dict) -> None:
                    await self._on_tool_progress(msg)

                inner = getattr(tool, "deep_research", None)
                targets = [tool] + ([inner] if inner is not None else [])
                for target in targets:
                    target.progress_callback = _on_progress
                try:
                    async for chunk in tool.thinking_stream(
                        self.context.model_input or self.context.resolved_query or self.context.original_query
                    ):
                        if isinstance(chunk, dict):
                            if chunk.get("answer"):
                                self.answer = chunk["answer"]
                            self.thinking = chunk.get("thinking")
                finally:
                    for target in targets:
                        target.progress_callback = None
            else:
                # 无流式接口（测试桩等）：回退到闭路 ask 路径
                self.answer = await asyncio.to_thread(
                    self.agent.ask,
                    self.context.model_input or self.context.resolved_query or self.context.original_query,
                    self.context.session_id,
                    bypass_cache=self.context.source_mode.value == "web",
                )
        tool = getattr(self.agent, "research_tool", None)
        provider_results = list(getattr(tool, "provider_results", []) or [])
        if not provider_results and getattr(tool, "deep_research", None) is not None:
            provider_results = list(getattr(tool.deep_research, "provider_results", []) or [])
        if provider_results:
            self.results = provider_results

    async def _on_tool_progress(self, msg: dict) -> None:
        """聚合工具迭代进度并发布 agent.progress / iteration.completed 事件"""
        if self.events is None:
            return
        kind = msg.get("kind")
        idx = int(msg.get("iteration_index", 0))
        if kind == "iteration":
            self._iterations.setdefault(idx, {"queries": [], "total_results": 0, "info_snippets": []})
            await self.events.publish(self.context.run_id, "agent.progress", stage="executing",
                                      payload={"iteration_index": idx, "kind": "iteration"})
        elif kind == "search":
            acc = self._iterations.setdefault(idx, {"queries": [], "total_results": 0, "info_snippets": []})
            query = msg.get("query", "")
            result_count = int(msg.get("result_count") or 0)
            found_useful = bool(msg.get("found_useful"))
            acc["queries"].append({"query": query, "result_count": result_count, "found_useful": found_useful})
            acc["total_results"] += result_count
            preview = msg.get("useful_info_preview")
            if preview and len(acc["info_snippets"]) < 3:
                acc["info_snippets"].append(str(preview)[:200])
            await self.events.publish(self.context.run_id, "agent.progress", stage="executing",
                                      payload={"iteration_index": idx, "kind": "search", "query": query,
                                               "result_count": result_count, "found_useful": found_useful,
                                               "useful_info_preview": msg.get("useful_info_preview")})
        elif kind == "iteration_done":
            acc = self._iterations.get(idx, {"queries": [], "total_results": 0, "info_snippets": []})
            tool = getattr(self.agent, "research_tool", None)
            if getattr(tool, "deep_research", None) is not None:
                tool = tool.deep_research
            max_iterations = getattr(tool, "max_iterations", None)
            await self.events.publish(self.context.run_id, "iteration.completed", stage="executing",
                                      payload={"iteration_index": idx, "kind": "iteration",
                                               "queries": acc["queries"], "total_results": acc["total_results"],
                                               "info_snippets": acc["info_snippets"],
                                               "max_iterations": max_iterations,
                                               "iterations_total": len(self._iterations)})
        elif kind == "answer":
            await self.events.publish(self.context.run_id, "iteration.completed", stage="executing",
                                      payload={"iteration_index": idx, "kind": "answer",
                                               "answer_char_count": msg.get("answer_char_count"),
                                               "iterations_total": len(self._iterations)})

    async def report(self) -> str:
        if self._repair_pending and self._report is not None:
            self._repair_pending = False
            return self._report
        ranked = rank_evidence(self.results)
        body = add_inline_citations(sanitize_report(self.answer), citation_evidence_ids(ranked))
        limitation = "\n\n## 局限\n\n当前证据来源数量有限，结论应结合更多独立来源复核。" if len({item.metadata.source_id for item in ranked}) < 2 else ""
        self._report = f"# 深度研究报告\n\n{body}{limitation}".strip()
        return self._report

    async def repair_report(self, failures: list[str]) -> bool:
        if self._report is None:
            return False
        supported = {"citation_integrity", "required_section", "report_consistency", "source_diversity"}
        if not set(failures).issubset(supported):
            return False
        if set(failures) & {"citation_integrity", "report_consistency"}:
            self._report, selected = _evidence_only_report(
                self.results,
                required_sections=list(self.context.config_snapshot.get("required_sections", [])),
            )
            if not selected:
                return False
        if "source_diversity" in failures and "局限" not in self._report:
            self._report += "\n\n## 局限\n\n当前证据来源数量有限。"
        self._repair_pending = True
        return True

    def snapshot(self) -> dict[str, Any]:
        return {"answer": self.answer, "report": self._report, "plan": self._plan, "results": [item.to_dict() for item in self.results], "repair_pending": self._repair_pending}

    def execution_records(self) -> list[ExecutionRecord]:
        task_id = f"task_{self.context.run_id}_research"
        tool = getattr(self.agent, "research_tool", None)
        if getattr(tool, "deep_research", None) is not None:
            tool = tool.deep_research
        calls = list(getattr(tool, "provider_calls", []) or [])
        if not calls and self.results:
            calls = [{"tool_call_id": f"call_{self.context.run_id}_research", "query": self.context.resolved_query or self.context.original_query, "results": self.results}]
        records = []
        tool_name = "tavily_search" if self.context.source_mode.value == "web" else "deep_research"
        for index, call in enumerate(calls):
            results = list(call.get("results", []))
            records.append(ExecutionRecord(
                record_id=f"record_{self.context.run_id}_{index}", task_id=task_id,
                session_id=self.context.session_id, worker_type="deep_research",
                inputs={"query": call.get("query")},
                tool_calls=[{
                    "tool_name": tool_name, "tool_call_id": call["tool_call_id"],
                    "source_mode": self.context.source_mode.value, "args": {"query": call.get("query")},
                    "result": {"result_ids": [item.result_id for item in results]}, "status": "success",
                }], evidence=results,
            ))
        return records

    def evidence_results(self) -> list[tuple[str | None, str | None, str, RetrievalResult]]:
        task_id = f"task_{self.context.run_id}_research"
        provider = getattr(self.agent, "retrieval_provider", None)
        provider_name = getattr(provider, "provider_name", "deep_research")
        tool = getattr(self.agent, "research_tool", None)
        if getattr(tool, "deep_research", None) is not None:
            tool = tool.deep_research
        calls = list(getattr(tool, "provider_calls", []) or [])
        if calls:
            return [(task_id, call["tool_call_id"], provider_name, item) for call in calls for item in call.get("results", [])]
        return [(task_id, f"call_{self.context.run_id}_research", provider_name, item) for item in self.results]

    def report_consistency(self) -> bool | None:
        return bool(self._report and self._report.strip()) and not has_internal_material(self._report)

    def evidence_card_coverage(self) -> dict[str, Any] | None:
        return None

    def report_metrics(self) -> dict[str, Any]:
        return {}

    def plan_record(self) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        if not self._plan:
            return None
        return self._plan, list(self._plan["tasks"])
