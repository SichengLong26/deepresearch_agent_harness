"""Run control, durable event streaming, evidence and report reads."""

import hashlib
import json
import re

from fastapi import APIRouter, Depends, Header, Query, Request, status
from sse_starlette.sse import EventSourceResponse
from pydantic import BaseModel, Field

from backend.app.dependencies import get_database, get_event_stream, get_run_service
from backend.app.schemas import ClarificationSubmit, RunControl
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EvidenceRepository,
    MessageRepository, RunRepository,
)
from deepresearch_agent.context import ArtifactEditContextBuilder

router = APIRouter(prefix="/runs", tags=["runs"])


class CommandSubmit(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    content: str | None = Field(default=None, max_length=20000)
    command: dict | None = None
    preview_only: bool = False
    base_revision: int | None = None
    base_checkpoint_version: int | None = None


class CommandApply(BaseModel):
    preview_hash: str | None = None


@router.get("/{run_id}/edit-state")
async def get_edit_state(run_id: str, database=Depends(get_database)):
    from sqlalchemy import select
    from deepresearch_agent.persistence.models import RunEditModel
    await require_run(database, run_id)
    checkpoint = await CheckpointRepository(database).latest(run_id)
    async with database.sessions() as session:
        row = (await session.execute(select(RunEditModel).where(RunEditModel.run_id == run_id)
            .order_by(RunEditModel.created_at.desc()).limit(1))).scalar_one_or_none()
    return {"plan": None if not checkpoint else json.loads(checkpoint.state_json).get("workflow_state", {}).get("state", {}).get("plan"),
        "command": None if not row else {"request_id": row.request_id, "status": row.status, **json.loads(row.result_json)}}


@router.post("/{run_id}/commands", status_code=202)
async def submit_command(run_id: str, payload: CommandSubmit, run_service=Depends(get_run_service)):
    return await run_service.commands.submit(run_id, payload.request_id, payload.model_dump(exclude={"request_id"}, exclude_none=True))


@router.get("/{run_id}/commands/{request_id}")
async def get_command(run_id: str, request_id: str, run_service=Depends(get_run_service)):
    return await run_service.commands.get(run_id, request_id)


@router.post("/{run_id}/commands/{request_id}/apply")
async def apply_command(run_id: str, request_id: str, payload: CommandApply, run_service=Depends(get_run_service)):
    return await run_service.commands.apply(run_id, request_id, payload.preview_hash)


@router.post("/{run_id}/commands/{request_id}/cancel")
async def cancel_command(run_id: str, request_id: str, run_service=Depends(get_run_service)):
    return await run_service.commands.cancel(run_id, request_id)


@router.post("/{run_id}/commands/{request_id}/clarifications")
async def clarify_command(run_id: str, request_id: str, payload: ClarificationSubmit, run_service=Depends(get_run_service)):
    return await run_service.commands.clarify(run_id, request_id, payload.content)


@router.get("/{run_id}/revisions/{revision}")
async def get_revision(run_id: str, revision: int, database=Depends(get_database)):
    from deepresearch_agent.persistence.models import RunRevisionModel
    async with database.sessions() as session:
        row = await session.get(RunRevisionModel, (run_id, revision))
        if not row:
            raise AppError(ErrorCode.NOT_FOUND, "修订版本不存在")
        return {"revision": revision, "snapshot": json.loads(row.snapshot_json), "impact": json.loads(row.impact_json)}


@router.get("/{run_id}/revisions")
async def get_revisions(run_id: str, database=Depends(get_database)):
    from sqlalchemy import select
    from deepresearch_agent.persistence.models import RunRevisionModel
    await require_run(database, run_id)
    async with database.sessions() as session:
        rows = (await session.execute(select(RunRevisionModel).where(RunRevisionModel.run_id == run_id)
            .order_by(RunRevisionModel.revision))).scalars()
        return {"items": [{"revision": r.revision, "impact": json.loads(r.impact_json),
            "plan": json.loads(r.snapshot_json).get("workflow_state", {}).get("state", {}).get("plan")} for r in rows]}

_EVIDENCE_ANNEX = re.compile(r"(?ms)^##\s+全量证据索引\s*.*$")


def _report_presentation(content: str | None, *, evidence_count: int = 0) -> tuple[str | None, str, list[dict[str, object]]]:
    """Separate the readable report from legacy inline evidence annexes."""
    if not content:
        return None, "normal", []
    report_mode = "budget_fallback" if "预算保护模式" in content[:240] else "normal"
    body = _EVIDENCE_ANNEX.sub("", content).strip()
    if report_mode == "budget_fallback":
        body = _compact_fallback_report(body, evidence_count=evidence_count)
    sections: list[dict[str, object]] = []
    for index, match in enumerate(re.finditer(r"(?m)^(#{1,4})\s+(.+?)\s*$", body)):
        title = match.group(2).strip()
        sections.append({"id": f"report-section-{index}", "title": title, "level": len(match.group(1))})
    return body, report_mode, sections


def _compact_fallback_report(content: str, *, evidence_count: int) -> str:
    """Make legacy deterministic card dumps readable without mutating history."""
    output: list[str] = []
    kept_in_section = 0
    omitted_in_section = 0

    def flush_omitted() -> None:
        nonlocal omitted_in_section
        if omitted_in_section:
            output.extend(["", f"本节另有 {omitted_in_section} 条证据保存在“证据”视图中。"])
            omitted_in_section = 0

    for raw in content.splitlines():
        stripped = raw.strip()
        if stripped.startswith("## "):
            flush_omitted()
            kept_in_section = 0
            title = stripped[3:].strip()
            output.extend(["", f"## {title if len(title) <= 46 else title[:46].rstrip() + '…'}"])
            continue
        if raw.startswith("- "):
            if kept_in_section >= 4:
                omitted_in_section += 1
                continue
            citations = re.findall(r"\[(?:\^)?ev_[A-Za-z0-9_-]+\]", stripped)
            claim = re.sub(r"\[(?:\^)?ev_[A-Za-z0-9_-]+\]", "", stripped[2:])
            claim = _evidence_summary(re.sub(r"[`#>*_|]+", " ", claim), limit=360)
            if not claim:
                continue
            output.append(f"- {claim}{(' ' + ' '.join(citations)) if citations else ''}")
            kept_in_section += 1
            continue
        if raw.startswith("  - "):
            continue
        output.append(raw.rstrip())
    flush_omitted()
    if evidence_count:
        output.extend(["", f"> 完整证据台账共 {evidence_count} 条，请在报告顶部切换到“证据”视图核查来源与摘要。"])
    return re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()


def _evidence_summary(value: str | None, *, limit: int = 480) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(
        r"(?i)skip to content|navigation menu|sign in|cookie settings|loading(?:\s+loading)+|documentation index",
        " ",
        text,
    )
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def run_dict(run):
    config = json.loads(run.config_snapshot_json or "{}")
    return {
        "run_id": run.run_id, "session_id": run.session_id, "trigger_message_id": run.trigger_message_id,
        "source_mode": run.source_mode, "workflow_mode": run.workflow_mode, "status": run.status,
        "current_stage": run.current_stage, "usage": json.loads(run.usage_json or "{}"),
        "error_code": run.error_code, "error_message": run.error_message,
        "cancellation_requested": bool(run.cancellation_requested),
        "pause_requested": bool(config.get("pause_requested")), "created_at": run.created_at,
        "started_at": run.started_at, "completed_at": run.completed_at, "updated_at": run.updated_at,
    }


async def require_run(database, run_id: str):
    run = await RunRepository(database).get(run_id)
    if run is None:
        raise AppError(ErrorCode.NOT_FOUND, "Run 不存在")
    return run


@router.get("/{run_id}")
async def get_run(run_id: str, database=Depends(get_database)):
    return run_dict(await require_run(database, run_id))


@router.post("/{run_id}/cancel", response_model=RunControl)
async def cancel_run(run_id: str, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.cancel(run_id):
        raise AppError(ErrorCode.CONFLICT, "Run 已结束或当前不能取消")
    return RunControl(run_id=run_id, status="cancelling")


@router.post("/{run_id}/pause", response_model=RunControl)
async def pause_run(run_id: str, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.pause(run_id):
        raise AppError(ErrorCode.CONFLICT, "Run 已结束、已暂停或当前不能暂停")
    return RunControl(run_id=run_id, status="pausing")


@router.post("/{run_id}/resume", response_model=RunControl)
async def resume_run(run_id: str, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.resume(run_id):
        raise AppError(ErrorCode.CONFLICT, "Run 当前不能恢复")
    return RunControl(run_id=run_id, status="resuming")


@router.post("/{run_id}/clarifications", response_model=RunControl)
async def clarify_run(run_id: str, payload: ClarificationSubmit, database=Depends(get_database), run_service=Depends(get_run_service)):
    await require_run(database, run_id)
    if not await run_service.resume(run_id, clarification=payload.content):
        raise AppError(ErrorCode.CONFLICT, "Run 当前不等待澄清")
    return RunControl(run_id=run_id, status="queued")


@router.get("/{run_id}/events")
async def run_events(
    run_id: str, request: Request, after_event_id: int = Query(0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    database=Depends(get_database), event_stream=Depends(get_event_stream),
):
    await require_run(database, run_id)
    cursor = after_event_id
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))
    return EventSourceResponse(event_stream.stream(run_id, after_event_id=cursor), ping=None, headers={"Cache-Control": "no-cache"})


@router.get("/{run_id}/evidence")
async def get_evidence(run_id: str, database=Depends(get_database)):
    await require_run(database, run_id)
    items = await EvidenceRepository(database).list_for_run(run_id)
    return {"items": [
        {"evidence_id": item.evidence_id, "run_id": item.run_id, "task_id": item.task_id, "tool_call_id": item.tool_call_id,
         "source_mode": item.source_mode, "provider": item.provider, "source_id": item.source_id, "title": item.title,
         "summary": item.summary, "metadata": json.loads(item.metadata_json or "{}"), "content_hash": item.content_hash,
         "artifact_id": item.artifact_id, "score": item.score, "created_at": item.created_at}
        for item in items
    ], "total": len(items)}


@router.get("/{run_id}/report")
async def get_report(run_id: str, database=Depends(get_database)):
    run = await require_run(database, run_id)
    message = await MessageRepository(database).get_assistant_for_run(run_id)
    artifact = await ArtifactRepository(database).get_report(run_id)
    checks = await ContractRepository(database).list_for_run(run_id)
    evidence = await EvidenceRepository(database).list_for_run(run_id)
    content, report_mode, sections = _report_presentation(message.content if message else None, evidence_count=len(evidence))
    return {
        "run_id": run_id, "status": run.status, "content": content,
        "report_mode": report_mode, "sections": sections,
        "evidence_index": [
            {
                "evidence_id": item.evidence_id,
                "title": item.title,
                "source_id": item.source_id,
                "source_mode": item.source_mode,
                "provider": item.provider,
                "summary": _evidence_summary(item.summary),
                "url": (json.loads(item.metadata_json or "{}").get("url") or item.source_id),
                "score": item.score,
            }
            for item in evidence
        ],
        "artifact": None if artifact is None else {"artifact_id": artifact.artifact_id, "relative_path": artifact.relative_path, "sha256": artifact.sha256, "size_bytes": artifact.size_bytes},
        "verification": [
            {"check_id": check.check_id, "kind": check.kind, "required": bool(check.required), "passed": None if check.passed is None else bool(check.passed), "evidence": json.loads(check.evidence_json or "{}")}
            for check in checks
        ],
    }


@router.get("/{run_id}/context")
async def get_context_inspector(
    run_id: str, database=Depends(get_database), run_service=Depends(get_run_service),
):
    """Return the durable, structured Context Pack without exposing the raw model prompt."""
    run = await require_run(database, run_id)
    checkpoint = await CheckpointRepository(database).latest(run_id)
    if checkpoint is None:
        return {"run_id": run_id, "ready": False, "status": run.status}
    if not CheckpointRepository.verify(checkpoint):
        raise AppError(ErrorCode.CONFLICT, "Run 上下文 checkpoint 完整性校验失败")

    state = json.loads(checkpoint.state_json or "{}")
    snapshot = state.get("context_snapshot") or {}
    artifact_edit = snapshot.get("artifact_edit")
    artifact_verification = None
    if artifact_edit:
        message = await MessageRepository(database).get_assistant_for_run(run_id)
        current_sections = {
            section.heading: section.sha256
            for section in ArtifactEditContextBuilder.parse_sections(message.content if message else "")
        }
        preserved = []
        for section in artifact_edit.get("preserve_sections", []):
            actual = current_sections.get(section["heading"])
            preserved.append({
                "heading": section["heading"], "expected_sha256": section["sha256"],
                "actual_sha256": actual, "passed": actual == section["sha256"],
            })
        target_before = hashlib.sha256(artifact_edit.get("target_content", "").encode("utf-8")).hexdigest()
        target_after = current_sections.get(artifact_edit.get("target_heading"))
        artifact_verification = {
            "available": message is not None,
            "target_heading": artifact_edit.get("target_heading"),
            "target_changed": bool(target_after and target_after != target_before),
            "preserved_sections": preserved,
            "all_preserved": bool(preserved) and all(item["passed"] for item in preserved),
        }

    return {
        "run_id": run_id, "ready": bool(snapshot), "status": run.status,
        "checkpoint": {
            "version": checkpoint.version, "stage": checkpoint.stage,
            "state_hash": checkpoint.state_hash, "verified": True,
            "created_at": checkpoint.created_at,
        },
        "stable_snapshot_id": snapshot.get("stable_snapshot_id"),
        "memory_snapshot_version": snapshot.get("memory_snapshot_version", 0),
        "stable_blocks": snapshot.get("stable_blocks", []),
        "dynamic_blocks": snapshot.get("dynamic_blocks", []),
        "retrieval_trace": snapshot.get("retrieval_trace", []),
        "token_usage_by_block": snapshot.get("token_usage_by_block", {}),
        "total_input_tokens": snapshot.get("total_input_tokens", 0),
        "curated_memory": snapshot.get("curated_memory", []),
        "recent_messages": snapshot.get("recent_messages", []),
        "session_summary": snapshot.get("session_summary") or {},
        "historical_recall_searched": bool(snapshot.get("historical_recall_searched")),
        "historical_recall": snapshot.get("historical_recall", []),
        "used_session_ids": snapshot.get("used_session_ids", []),
        "selected_skill": snapshot.get("selected_skill"),
        "active_skill_policy": snapshot.get("active_skill_policy"),
        "skill_policy_consumers": snapshot.get("skill_policy_consumers", []),
        "artifact_edit": artifact_edit,
        "artifact_verification": artifact_verification,
    }
