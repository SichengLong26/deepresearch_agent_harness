"""Validated, deterministic plan edits. No model or persistence side effects."""
from __future__ import annotations

import copy
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec, TaskNode
from .contracts import RunStatus
from .run_context import RunContext


class EditOperation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["update_task", "skip_task", "add_task", "rerun_task", "update_report_requirements"]
    task_id: str | None = None
    changes: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    dependents_policy: Literal["block", "cascade_skip"] = "block"


class EditCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["modify", "modify_and_resume", "resume", "clarify", "inspect"]
    operations: list[EditOperation] = Field(default_factory=list, max_length=30)
    explanation: str = ""


def revise(context: RunContext, command: EditCommand) -> tuple[RunContext, dict]:
    """Return a new snapshot and impact; the caller atomically commits both."""
    candidate = context.model_copy(deep=True)
    command = command.model_copy(deep=True)
    state = candidate.workflow_state.get("state", {})
    raw_plan = state.get("plan")
    if not raw_plan or context.workflow_mode.value != "plan_execute_report":
        raise ValueError("局部修改需要 PlanExecute 模式下已完成规划的 Run")
    if not command.operations:
        raise ValueError("未指定修改操作")
    old = PlanSpec.model_validate(raw_plan)
    plan = old.model_copy(deep=True)
    nodes = {n.task_id: n for n in plan.task_graph.nodes}
    roots, skipped = set(), set()
    report_changes = {}
    exclusions = list(candidate.config_snapshot.get("scope_exclusions", []))
    allowed = {"description", "parameters", "entities", "task_type", "depends_on", "priority", "estimated_tokens"}
    aliases = {}
    for index, op in enumerate(command.operations):
        if op.type == "add_task":
            alias = op.task_id or f"new_{index}"
            if alias in nodes or alias in aliases:
                raise ValueError("新增任务临时 ID 重复")
            aliases[alias] = "task_" + uuid.uuid5(uuid.NAMESPACE_URL, f"{context.run_id}:{context.plan_version}:{alias}").hex[:24]
            op.task_id = aliases[alias]
    for op in command.operations:
        if op.type != "add_task":
            op.task_id = aliases.get(op.task_id, op.task_id)
        if "depends_on" in op.changes:
            op.changes["depends_on"] = [aliases.get(dep, dep) for dep in op.changes["depends_on"]]
    # Allocate all additions first, allowing forward references within this batch.
    for op in command.operations:
        if op.type == "add_task":
            if set(op.changes) - allowed:
                raise ValueError("新增任务包含不允许的字段")
            task = TaskNode(**op.changes, source_mode=plan.source_mode,
                            **({"task_id": op.task_id} if op.task_id else {}))
            if task.task_id in nodes:
                raise ValueError("新增任务 ID 已存在")
            nodes[task.task_id] = task
            roots.add(task.task_id)
    for op in command.operations:
        if op.type == "add_task":
            continue
        if op.type == "update_report_requirements":
            if set(op.changes) - {"language", "format", "length", "sections"}:
                raise ValueError("报告修改仅支持语言、格式、篇幅和章节")
            if any(not isinstance(value, (str, list)) for value in op.changes.values()):
                raise ValueError("报告要求必须为文本或列表")
            report_changes.update(op.changes)
            continue
        if op.task_id not in nodes:
            raise ValueError(f"任务不存在：{op.task_id}")
        task = nodes[op.task_id]
        if op.type == "update_task":
            if set(op.changes) - allowed:
                raise ValueError("修改包含不允许的任务字段")
            changed = {k for k, v in op.changes.items() if getattr(task, k) != v}
            nodes[task.task_id] = TaskNode.model_validate({**task.model_dump(), **op.changes})
            if changed - {"priority", "estimated_tokens"}:
                roots.add(task.task_id)
        else:
            roots.add(task.task_id)
            if op.type == "skip_task":
                skipped.add(task.task_id)
                exclusions.append(task.description)
    plan.task_graph.nodes = list(nodes.values())
    plan.validate()
    edges = {key: set() for key in nodes}
    for graph in (old.task_graph, plan.task_graph):
        for task in graph.nodes:
            for dep in task.depends_on:
                edges.setdefault(dep, set()).add(task.task_id)

    def closure(seeds):
        affected = set(seeds)
        queue = list(seeds)
        while queue:
            for child in edges.get(queue.pop(), ()):
                if child not in affected:
                    affected.add(child)
                    queue.append(child)
        return affected

    affected = closure(roots)
    for op in command.operations:
        if op.type == "skip_task" and op.dependents_policy == "cascade_skip":
            skipped.update(closure({op.task_id}))
    # Use the new graph for blocking: an explicitly removed dependency is resolved.
    blocked = set()
    while True:
        new_blocked = {n.task_id for n in nodes.values() if n.task_id not in skipped
                       and any(d in skipped | blocked for d in n.depends_on)}
        if new_blocked <= blocked:
            break
        blocked |= new_blocked
    if blocked:
        raise ValueError("跳过会阻塞依赖任务：" + ", ".join(sorted(blocked)) + "；请明确级联跳过或修改依赖")
    prior = {n.task_id: n for n in old.task_graph.nodes}
    for task in nodes.values():
        if task.task_id in affected:
            task.task_revision = prior[task.task_id].task_revision + 1 if task.task_id in prior else 1
            task.status = "skipped" if task.task_id in skipped else "pending"
    if all(n.status == "skipped" for n in nodes.values()):
        raise ValueError("不能跳过全部研究任务；请保留研究范围或取消 Run")
    if skipped:
        descriptions = [nodes[task_id].description for task_id in sorted(skipped)]
        precedence = "修订优先：明确跳过的范围不参与执行、报告覆盖或完成度判定：" + "；".join(descriptions)
        plan.acceptance_criteria.completion_conditions = [
            precedence,
            "完成当前修订中所有未跳过的任务，并仅使用当前修订仍有效的结果和证据。",
            *plan.acceptance_criteria.completion_conditions,
        ]
    plan.version = max(context.plan_version, old.version) + 1
    plan.status = "approved"
    candidate.plan_version = plan.version
    state["plan"] = plan.model_dump(mode="json")
    planner = candidate.workflow_state.get("planner_result")
    if not planner:
        raise ValueError("检查点缺少 PlannerResult，无法安全修订")
    planner["plan_spec"] = copy.deepcopy(state["plan"])
    planner["executor_signal"] = plan.to_execution_signal().model_dump(mode="json")
    records = state.get("execution_records", [])
    state["execution_records"] = [r for r in records if r.get("task_id") not in affected]
    execution = state.get("execution_context") or {}
    execution["completed_task_ids"] = [n.task_id for n in nodes.values() if n.status == "completed"]
    for key in ("retrieval_cache", "intermediate_results", "reflection_retry_counts"):
        execution[key] = {k: v for k, v in execution.get(key, {}).items() if k not in affected}
    # Shared registries are derived caches; rebuild through retained records.
    if affected:
        execution["evidence_registry"] = {}
        execution["tool_call_history"] = [v for v in execution.get("tool_call_history", []) if v.get("task_id") not in affected]
        execution["errors"] = [v for v in execution.get("errors", []) if v.get("task_id") not in affected]
    state["execution_context"] = execution
    state["response"] = None
    state["report_context"] = None
    candidate.workflow_state["report_result"] = None
    candidate.workflow_state["repair_pending"] = False
    candidate.workflow_state.pop("verification_failures", None)
    candidate.report = None
    candidate.config_snapshot["report_requirements"] = {**candidate.config_snapshot.get("report_requirements", {}), **report_changes}
    candidate.config_snapshot["scope_exclusions"] = list(dict.fromkeys(exclusions))
    pending = [n.task_id for n in nodes.values() if n.status not in {"completed", "skipped"}]
    candidate.resume_from_status = RunStatus.EXECUTING if pending else RunStatus.REPORTING
    candidate.resume_cursor = candidate.resume_from_status
    candidate.status = RunStatus.PAUSED
    return candidate, {"revision": plan.version, "invalidated": sorted(affected),
        "skipped": sorted(skipped), "pending": pending,
        "reused": [n.task_id for n in nodes.values() if n.status == "completed"],
        "resume_stage": candidate.resume_cursor.value, "report_invalidated": True}
