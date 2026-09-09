"""Repositories for checkpoints, evidence, contracts and artifact metadata."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from sqlalchemy import func, select, update, text

from deepresearch_agent.harness.contracts import ContractCheckData, EvidenceData
from deepresearch_agent.harness.versioning import CHECKPOINT_SCHEMA_VERSION
from deepresearch_agent.persistence.artifact_store import StoredArtifact
from deepresearch_agent.persistence.database import Database
from deepresearch_agent.persistence.models import ArtifactModel, CheckpointModel, ContractCheckModel, EvidenceModel, PlanModel, TaskModel, ToolCallModel, RunModel

from .utils import json_text, new_id, utc_now_iso


class PlanTaskToolRepository:
    """Persist a complete executable plan and idempotent tool-call intent/result."""

    def __init__(self, database: Database):
        self.database = database

    async def save_plan(self, *, run_id: str, plan_id: str, version: int, status: str, plan: dict[str, Any], tasks: list[dict[str, Any]], source_mode: str) -> PlanModel:
        now = utc_now_iso()
        async with self.database.transaction() as session:
            plan_model = await session.get(PlanModel, plan_id)
            if plan_model is None:
                plan_model = PlanModel(plan_id=plan_id, run_id=run_id, version=version, status=status, plan_json=json_text(plan), created_at=now, updated_at=now)
                session.add(plan_model)
                await session.flush()
            else:
                plan_model.version = version
                plan_model.status = status
                plan_model.plan_json = json_text(plan)
                plan_model.updated_at = now
            for item in tasks:
                task_model = await session.get(TaskModel, item["task_id"])
                if task_model is None:
                    session.add(TaskModel(task_id=item["task_id"], run_id=run_id, plan_id=plan_id, task_type=item["task_type"], source_mode=source_mode, status=item.get("status", "pending"), task_json=json_text(item), created_at=now, updated_at=now))
                else:
                    task_model.status = item.get("status", task_model.status)
                    task_model.task_json = json_text(item)
                    task_model.updated_at = now
            return plan_model

    async def prepare_tool_call(self, *, tool_call_id: str, run_id: str, task_id: Optional[str], tool_name: str, source_mode: str, args: dict[str, Any]) -> tuple[ToolCallModel, bool]:
        async with self.database.transaction() as session:
            existing = await session.get(ToolCallModel, tool_call_id)
            if existing:
                return existing, False
            model = ToolCallModel(tool_call_id=tool_call_id, run_id=run_id, task_id=task_id, tool_name=tool_name, source_mode=source_mode, status="prepared", args_json=json_text(args), created_at=utc_now_iso())
            session.add(model)
            return model, True

    async def complete_tool_call(self, tool_call_id: str, *, result: Any = None, error_code: Optional[str] = None) -> bool:
        values = {"status": "failed" if error_code else "completed", "result_json": json_text(result) if result is not None else None, "error_code": error_code, "completed_at": utc_now_iso()}
        async with self.database.transaction() as session:
            outcome = await session.execute(update(ToolCallModel).where(ToolCallModel.tool_call_id == tool_call_id, ToolCallModel.status != "completed").values(**values))
            return outcome.rowcount == 1

    async def get_tool_call(self, tool_call_id: str) -> Optional[ToolCallModel]:
        async with self.database.sessions() as session:
            return await session.get(ToolCallModel, tool_call_id)


class CheckpointRepository:
    def __init__(self, database: Database):
        self.database = database

    async def save(self, run_id: str, stage: str, state: dict[str, Any], *, lease_owner: str | None = None) -> CheckpointModel:
        state_payload = {"schema_version": CHECKPOINT_SCHEMA_VERSION, **state}
        async with self.database.transaction() as session:
            if lease_owner is not None:
                await session.execute(text("BEGIN IMMEDIATE"))
                run = await session.get(RunModel, run_id)
                if not run or run.lease_owner != lease_owner or run.cancellation_requested:
                    from deepresearch_agent.harness.errors import LostExecutionLease
                    raise LostExecutionLease("执行权已失效，拒绝迟到任务检查点")
            if stage == "task_boundary":
                await self._commit_task_projection(session, run_id, state_payload)
            serialized = json_text(state_payload)
            digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
            current = (await session.execute(select(func.max(CheckpointModel.version)).where(CheckpointModel.run_id == run_id))).scalar_one()
            model = CheckpointModel(checkpoint_id=new_id("chk"), run_id=run_id, version=(current or 0) + 1, stage=stage, state_json=serialized, state_hash=digest, schema_version=CHECKPOINT_SCHEMA_VERSION, created_at=utc_now_iso())
            session.add(model)
            await session.flush()
            return model

    async def _commit_task_projection(self, session, run_id, snapshot):
        """Commit results and the resumable state together; old snapshots retain history."""
        from deepresearch_agent.agents.multi_agent.core.retrieval_result import RetrievalResult
        from deepresearch_agent.harness.evidence import EvidenceLedger
        workflow = snapshot.get("workflow_state", {}).get("state", {})
        plan = workflow.get("plan")
        if not plan:
            return
        projection = await session.get(PlanModel, plan["plan_id"])
        if projection:
            projection.plan_json = json_text(plan)
            projection.status = plan["status"]
            projection.updated_at = utc_now_iso()
        for task in plan["task_graph"]["nodes"]:
            model = await session.get(TaskModel, task["task_id"])
            if model:
                model.task_json, model.status = json_text(task), task["status"]
                model.updated_at = utc_now_iso()
        usage = snapshot.setdefault("budget_usage", {})
        for record in workflow.get("execution_records", []):
            for call in record.get("tool_calls", []):
                if await session.get(ToolCallModel, call["tool_call_id"]) is None:
                    session.add(ToolCallModel(tool_call_id=call["tool_call_id"], run_id=run_id,
                        task_id=record["task_id"], tool_name=call["tool_name"], source_mode=snapshot["source_mode"],
                        status="failed" if call.get("status") == "failed" else "completed",
                        args_json=json_text(call.get("args", {})), result_json=json_text(call.get("result")),
                        created_at=utc_now_iso(), completed_at=utc_now_iso()))
                    usage["tool_calls"] = usage.get("tool_calls", 0) + 1
                    if snapshot["source_mode"] == "web":
                        usage["tavily_calls"] = usage.get("tavily_calls", 0) + 1
            for raw in record.get("evidence", []):
                result = RetrievalResult.from_dict(raw)
                calls = record.get("tool_calls", [])
                _result, data = EvidenceLedger().assign(run_id=run_id, task_id=record["task_id"],
                    tool_call_id=calls[0]["tool_call_id"] if calls else None,
                    provider=calls[0]["tool_name"] if calls else "unknown", results=[result])[0]
                if await session.get(EvidenceModel, data.evidence_id) is None:
                    session.add(EvidenceModel(**data.model_dump(exclude={"source_mode"}), source_mode=data.source_mode.value,
                        metadata_json=json_text(result.metadata.model_dump(mode="json")), created_at=utc_now_iso()))
        run = await session.get(RunModel, run_id)
        if run:
            run.usage_json = json_text(usage)

    async def latest(self, run_id: str) -> Optional[CheckpointModel]:
        async with self.database.sessions() as session:
            return (await session.execute(select(CheckpointModel).where(CheckpointModel.run_id == run_id).order_by(CheckpointModel.version.desc()).limit(1))).scalar_one_or_none()

    @staticmethod
    def verify(checkpoint: CheckpointModel) -> bool:
        return hashlib.sha256(checkpoint.state_json.encode("utf-8")).hexdigest() == checkpoint.state_hash


class EvidenceRepository:
    def __init__(self, database: Database):
        self.database = database

    async def upsert(self, data: EvidenceData, *, metadata: Optional[dict[str, Any]] = None) -> EvidenceModel:
        async with self.database.transaction() as session:
            existing = await session.get(EvidenceModel, data.evidence_id)
            if existing:
                return existing
            model = EvidenceModel(evidence_id=data.evidence_id, run_id=data.run_id, task_id=data.task_id, tool_call_id=data.tool_call_id, source_mode=data.source_mode.value, provider=data.provider, source_id=data.source_id, title=data.title, summary=data.summary, metadata_json=json_text(metadata or {}), content_hash=data.content_hash, artifact_id=data.artifact_id, score=data.score, created_at=utc_now_iso())
            session.add(model)
            return model

    async def list_for_run(self, run_id: str) -> list[EvidenceModel]:
        async with self.database.sessions() as session:
            rows = list((await session.execute(select(EvidenceModel).where(EvidenceModel.run_id == run_id, EvidenceModel.invalidated_at.is_(None)).order_by(EvidenceModel.created_at))).scalars())
            run = await session.get(RunModel, run_id)
            if not run or not json.loads(run.config_snapshot_json).get("active_revision"):
                return rows
            checkpoint = (await session.execute(select(CheckpointModel).where(CheckpointModel.run_id == run_id)
                .order_by(CheckpointModel.version.desc()).limit(1))).scalar_one_or_none()
            if not checkpoint or not CheckpointRepository.verify(checkpoint):
                return []
            state = json.loads(checkpoint.state_json).get("workflow_state", {}).get("state", {})
            active_ids = set()
            for record in state.get("execution_records", []):
                for evidence in record.get("evidence", []):
                    metadata = evidence.get("metadata", {})
                    digest = metadata.get("content_hash") or hashlib.sha256(str(evidence.get("evidence", "")).encode()).hexdigest()
                    identity = f"{run_id}:{evidence.get('source_mode', run.source_mode)}:{metadata.get('source_id')}:{digest}"
                    active_ids.add("ev_" + hashlib.sha256(identity.encode()).hexdigest()[:24])
            return [row for row in rows if row.evidence_id in active_ids]


class ContractRepository:
    def __init__(self, database: Database):
        self.database = database

    async def upsert(self, data: ContractCheckData) -> ContractCheckModel:
        payload = {"observed": data.observed, "explanation": data.explanation, "artifact_refs": data.artifact_refs, "schema_version": data.schema_version}
        async with self.database.transaction() as session:
            model = await session.get(ContractCheckModel, data.check_id)
            if model is None:
                model = ContractCheckModel(check_id=data.check_id, run_id=data.run_id, kind=data.kind, required=int(data.required), threshold_json=json_text(data.threshold) if data.threshold is not None else None, verifier=data.verifier, verifier_version=data.verifier_version, passed=None if data.passed is None else int(data.passed), evidence_json=json_text(payload), created_at=utc_now_iso())
                session.add(model)
            else:
                model.passed = None if data.passed is None else int(data.passed)
                model.evidence_json = json_text(payload)
            return model

    async def required_checks_passed(self, run_id: str) -> bool:
        async with self.database.sessions() as session:
            checks = list((await session.execute(select(ContractCheckModel).where(ContractCheckModel.run_id == run_id, ContractCheckModel.required == 1))).scalars())
            return bool(checks) and all(check.passed == 1 for check in checks)

    async def list_for_run(self, run_id: str) -> list[ContractCheckModel]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(ContractCheckModel).where(ContractCheckModel.run_id == run_id).order_by(ContractCheckModel.kind)
            )
            return list(result.scalars())


class ArtifactRepository:
    def __init__(self, database: Database):
        self.database = database

    async def record(self, artifact: StoredArtifact, *, run_id: Optional[str] = None) -> ArtifactModel:
        async with self.database.transaction() as session:
            model = (await session.execute(select(ArtifactModel).where(ArtifactModel.relative_path == artifact.relative_path))).scalar_one_or_none()
            if model is None:
                model = ArtifactModel(artifact_id=artifact.artifact_id, run_id=run_id, relative_path=artifact.relative_path, mime_type=artifact.mime_type, size_bytes=artifact.size_bytes, sha256=artifact.sha256, created_at=utc_now_iso())
                session.add(model)
            else:
                model.run_id = run_id
                model.mime_type = artifact.mime_type
                model.size_bytes = artifact.size_bytes
                model.sha256 = artifact.sha256
            return model

    async def get_report(self, run_id: str) -> Optional[ArtifactModel]:
        async with self.database.sessions() as session:
            result = await session.execute(
                select(ArtifactModel)
                .where(ArtifactModel.run_id == run_id, ArtifactModel.mime_type.like("text/markdown%"))
                .order_by(ArtifactModel.created_at.desc())
            )
            return result.scalars().first()
