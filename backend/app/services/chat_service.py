"""Transactional chat command service."""

import re

from backend.app.schemas import MessageCreate, MessageSend, RunAccepted, RunCreate
from deepresearch_agent.config.settings import HARNESS_BUDGETS, LEARNING_REVIEW_MODEL, OPENAI_LLM_MODEL
from deepresearch_agent.harness.errors import AppError, ErrorCode
from deepresearch_agent.persistence.repositories import MessageRepository, RunRepository, SessionRepository


def is_resume_intent(content: str) -> bool:
    """Recognize short natural-language commands that mean resume, not a new task."""
    normalized = re.sub(r"[\s，。！？、,.!?;；:：]+", "", content).lower()
    if not normalized or len(normalized) > 60:
        return False
    if any(marker in normalized for marker in ("不要", "别继续", "别恢复", "取消", "停止", "重来", "重新规划")):
        return False
    if any(marker in normalized for marker in ("改", "跳过", "不执行", "换", "另一个", "新任务", "新增", "补充要求", "先", "不要", "skip")):
        return False
    direct = ("继续", "接着", "恢复", "续上", "往下", "下一步", "开始吧", "resume", "continue", "goon", "proceed")
    if any(marker in normalized for marker in direct):
        return True
    references = ("刚才", "之前", "原来", "上次", "当前", "这个任务", "该任务", "按计划")
    actions = ("执行", "运行", "开跑", "做下去", "完成它")
    return any(marker in normalized for marker in references) and any(action in normalized for action in actions)


class ChatService:
    def __init__(self, sessions: SessionRepository, runs: RunRepository, messages: MessageRepository, run_service):
        self.sessions = sessions
        self.runs = runs
        self.messages = messages
        self.run_service = run_service

    async def send(self, session_id: str, request: MessageSend) -> RunAccepted:
        session = await self.sessions.get(session_id)
        if session is None:
            raise AppError(ErrorCode.NOT_FOUND, "Session 不存在")
        if session.status != "active":
            raise AppError(ErrorCode.CONFLICT, "已归档 Session 不能发送消息")

        existing = await self.messages.get_by_client_id(session_id, request.client_message_id)
        if existing is not None and existing.run_id:
            existing_run = await self.runs.get(existing.run_id)
            if existing_run is not None:
                return RunAccepted(
                    message_id=existing.message_id, run_id=existing_run.run_id,
                    status=existing_run.status, events_url=f"/api/v1/runs/{existing_run.run_id}/events",
                    created=False,
                )

        paused = None if request.new_run else await self.runs.latest_paused_for_session(session_id)
        if request.target_run_id and not request.new_run:
            paused = await self.runs.get(request.target_run_id)
            if paused is None or paused.session_id != session_id:
                raise AppError(ErrorCode.NOT_FOUND, "目标 Run 不属于当前会话")
            if paused.status not in {"paused", "pausing"}:
                raise AppError(ErrorCode.CONFLICT, "目标 Run 当前不可修改")
        normalized_control = request.content.strip().rstrip("。！!")
        if paused is not None and normalized_control in {"取消", "取消任务", "取消这个任务", "取消研究"}:
            message, _ = await self.messages.append(MessageCreate(
                session_id=session_id, run_id=paused.run_id, role="user", content=request.content,
                client_message_id=request.client_message_id,
                metadata={"control": "cancel", "cancel_run_id": paused.run_id},
            ))
            cancelled = await self.run_service.cancel(paused.run_id)
            current = await self.runs.get(paused.run_id)
            if not cancelled and (current is None or current.status not in {"cancelling", "cancelled"}):
                raise AppError(ErrorCode.CONFLICT, "暂停的 Run 当前不能取消")
            return RunAccepted(
                message_id=message.message_id, run_id=paused.run_id,
                status=current.status if current else "cancelling",
                events_url=f"/api/v1/runs/{paused.run_id}/events", created=False,
            )
        if paused is not None and not is_resume_intent(request.content):
            command = await self.run_service.commands.submit_text(paused.run_id, request.client_message_id, request.content)
            message, _ = await self.messages.append(MessageCreate(
                session_id=session_id, run_id=paused.run_id, role="user", content=request.content,
                client_message_id=request.client_message_id, metadata={"command_id": command["request_id"]},
            ))
            return RunAccepted(message_id=message.message_id, run_id=paused.run_id, status=paused.status,
                events_url=f"/api/v1/runs/{paused.run_id}/events", created=False, command_id=command["request_id"])
        if paused is not None and is_resume_intent(request.content):
            message, _ = await self.messages.append(MessageCreate(
                session_id=session_id, run_id=paused.run_id, role="user",
                content=request.content, client_message_id=request.client_message_id,
                metadata={"control": "resume", "resume_run_id": paused.run_id},
            ))
            resumed = await self.run_service.resume(paused.run_id)
            current = await self.runs.get(paused.run_id)
            if not resumed and (current is None or current.status == "paused"):
                raise AppError(ErrorCode.CONFLICT, "暂停的 Run 当前不能恢复")
            return RunAccepted(
                message_id=message.message_id, run_id=paused.run_id,
                status="resuming" if resumed else current.status,
                events_url=f"/api/v1/runs/{paused.run_id}/events", created=False,
            )

        detailed_request = request.report_type == "long_document" or any(
            marker in request.content for marker in ("详细", "深入", "全面", "长篇")
        )
        message, run, created = await self.runs.create_for_user_message(
            MessageCreate(
                session_id=session_id, role="user", content=request.content,
                client_message_id=request.client_message_id,
                metadata={"source_mode": request.source_mode.value, "workflow_mode": request.workflow_mode.value},
            ),
            RunCreate(
                session_id=session_id, trigger_message_id="assigned-atomically",
                source_mode=request.source_mode, workflow_mode=request.workflow_mode,
                config_snapshot={
                    # A single weakly-related chunk may be enough for a concise
                    # answer, but must never unlock a user-requested detailed report.
                    "min_evidence": 3 if detailed_request else 1,
                    "report_type": "long_document" if detailed_request else request.report_type,
                    "deep_research_max_iterations": 1 if request.source_mode.value == "graphrag" else 2,
                    "schema_version": 1,
                },
                budget=HARNESS_BUDGETS,
            ),
        )
        if created:
            await self.runs.update_model_snapshot(run.run_id, {
                "llm_model": OPENAI_LLM_MODEL,
                "learning_review_model": LEARNING_REVIEW_MODEL,
            })
            if session.title.strip() in {"新对话", "New conversation", "Untitled"}:
                title = " ".join(request.content.strip().split())[:36]
                if title:
                    await self.sessions.rename(session_id, title)
            self.run_service.schedule(run.run_id)
        return RunAccepted(
            message_id=message.message_id, run_id=run.run_id, status=run.status,
            events_url=f"/api/v1/runs/{run.run_id}/events", created=created,
        )
