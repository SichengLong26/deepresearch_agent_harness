"""Single-process background scheduler connected to the persistent Harness."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from deepresearch_agent.agents.deep_research_agent import DeepResearchAgent
from deepresearch_agent.agents.multi_agent.integration.multi_agent_factory import MultiAgentFactory
from deepresearch_agent.agents.multi_agent.executor.worker_coordinator import WorkerCoordinator
from deepresearch_agent.config.settings import ARTIFACT_ROOT, AUTO_RESUME_RUNS
from deepresearch_agent.harness.contracts import SourceMode, WorkflowMode
from deepresearch_agent.harness.event_bus import EventBus
from deepresearch_agent.harness.recovery import RecoveryManager
from deepresearch_agent.harness.runtime import HarnessRuntime
from deepresearch_agent.harness.workflow import DeepResearchDriver, PlanExecuteReportDriver
from deepresearch_agent.persistence import ArtifactStore, Database
from deepresearch_agent.persistence.repositories import (
    ArtifactRepository, CheckpointRepository, ContractRepository, EventRepository,
    EvidenceRepository, MessageRepository, PlanTaskToolRepository, RunRepository,
    SessionRepository, MemoryRepository, AuditRepository,
    LearningReviewRepository, SkillRepository,
)
from deepresearch_agent.context import ArtifactEditContextBuilder, ContextBuilder, ContextCompactor
from deepresearch_agent.memory import MemoryExtractor, MemoryService
from deepresearch_agent.sessions import SessionSearchService
from deepresearch_agent.config import settings
from deepresearch_agent.evolution import PromotionPolicy, SkillLearningService, SkillLoader, SkillRegistry
from deepresearch_agent.models.prefix_cache import clear_run_cancelled, mark_run_cancelled, tracker as prefix_tracker
from deepresearch_agent.models.get_models import get_llm_model
from deepresearch_agent.retrieval.router import create_default_router
from deepresearch_agent.retrieval.base import TimeoutBoundProvider
from backend.app.schemas import MessageCreate, RunCreate, SessionCreate


class RunService:
    def __init__(
        self,
        database: Database,
        *,
        workflow_factory: Callable[[Any], Any] | None = None,
        artifact_root=ARTIFACT_ROOT,
        skills_root=settings.SKILLS_ROOT,
    ):
        self.database = database
        self.runs = RunRepository(database)
        self.messages = MessageRepository(database)
        self.events = EventRepository(database)
        self.event_bus = EventBus(self.events)
        self.artifact_store = ArtifactStore(artifact_root)
        self._external_factory = workflow_factory
        self._router = None if workflow_factory else create_default_router()
        self._tasks: dict[str, asyncio.Task] = {}
        from backend.app.services.run_command_service import RunCommandService
        self.commands = RunCommandService(self)
        memory_repository = MemoryRepository(database)
        memory_service = MemoryService(
            memory_repository, AuditRepository(database),
            user_max_tokens=settings.MEMORY_USER_MAX_TOKENS,
            project_max_tokens=settings.MEMORY_PROJECT_MAX_TOKENS,
            user_max_chars=settings.MEMORY_USER_MAX_CHARS,
            project_max_chars=settings.MEMORY_PROJECT_MAX_CHARS,
        )
        session_repository = SessionRepository(database)
        self.sessions = session_repository
        artifact_repository = ArtifactRepository(database)
        skill_repository = SkillRepository(database)
        self.skill_registry = SkillRegistry(skills_root, skill_repository)
        skill_loader = SkillLoader(self.skill_registry)
        self.context_builder = ContextBuilder(
            messages=self.messages, sessions=session_repository, memory_service=memory_service,
            session_search=SessionSearchService(self.messages, session_repository),
            compactor=ContextCompactor(
                session_repository, self.messages,
                threshold_tokens=min(
                    settings.CONTEXT_COMPRESSION_THRESHOLD_TOKENS,
                    max(100, int(settings.CONTEXT_MAX_TOKENS * settings.CONTEXT_COMPRESSION_THRESHOLD_RATIO)),
                ),
                protect_recent_messages=settings.CONTEXT_PROTECT_RECENT_MESSAGES,
                target_tokens=settings.CONTEXT_COMPRESSION_TARGET_TOKENS,
            ),
            skill_loader=skill_loader,
            artifact_context_builder=ArtifactEditContextBuilder(
                self.runs, artifact_repository, self.artifact_store,
            ),
            max_tokens=settings.CONTEXT_MAX_TOKENS,
            max_chars=settings.CONTEXT_MAX_CHARS,
            recent_turns=settings.CONTEXT_RECENT_TURNS,
            session_search_top_k=settings.SESSION_SEARCH_TOP_K,
            session_search_window=settings.SESSION_SEARCH_WINDOW,
        )
        memory_llm = None
        if (
            settings.MEMORY_BACKGROUND_REVIEW_ENABLED
            and workflow_factory is None
            and settings.OPENAI_API_KEY
            and settings.MEMORY_LLM_MODEL
        ):
            memory_llm = get_llm_model(
                model=settings.MEMORY_LLM_MODEL, temperature=0.0,
                max_tokens=settings.MEMORY_LLM_MAX_OUTPUT_TOKENS,
            )
        self.memory_extractor = None if memory_llm is None else MemoryExtractor(
            memory_service, self.runs, self.messages, ContractRepository(database),
            llm=memory_llm,
            min_confidence=settings.MEMORY_LLM_MIN_CONFIDENCE,
            max_candidates=settings.MEMORY_LLM_MAX_CANDIDATES,
            max_message_chars=settings.MEMORY_LLM_MAX_MESSAGE_CHARS,
        )
        learning_llms = []
        if settings.LEARNING_REVIEW_ENABLED and workflow_factory is None and settings.OPENAI_API_KEY and settings.LEARNING_REVIEW_MODEL:
            learning_llms = [
                get_llm_model(model=settings.LEARNING_REVIEW_MODEL, temperature=0.0, max_tokens=settings.LEARNING_REVIEW_MAX_OUTPUT_TOKENS),
                get_llm_model(model=settings.LEARNING_REVIEW_MODEL, temperature=0.0, max_tokens=settings.LEARNING_REVIEW_MAX_OUTPUT_TOKENS),
            ]
        self.skill_learning = SkillLearningService(
            database, LearningReviewRepository(database), skill_repository, self.skill_registry,
            proposer_llm=learning_llms[0] if learning_llms else None,
            critic_llm=learning_llms[1] if learning_llms else None,
            events=self.event_bus, enabled=settings.LEARNING_REVIEW_ENABLED,
            max_retries=settings.LEARNING_REVIEW_MAX_RETRIES,
        )
        self.skill_promotion = PromotionPolicy(
            skill_repository, self.skill_registry, AuditRepository(database),
            require_real_replay=True, staged=True,
            canary_percent=settings.SKILL_CANARY_INITIAL_PERCENT,
        )

    def schedule(self, run_id: str) -> asyncio.Task:
        existing = self._tasks.get(run_id)
        if existing is not None and not existing.done():
            return existing
        clear_run_cancelled(run_id)
        task = asyncio.create_task(self._execute(run_id), name=f"run:{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))
        return task

    async def _execute(self, run_id: str):
        agents: list[Any] = []

        def workflow_factory(context, events=None):
            if self._external_factory is not None:
                return self._external_factory(context, events)
            provider = TimeoutBoundProvider(
                self._router.for_mode(context.source_mode),
                timeout_seconds=context.budget_limits.tool_timeout_seconds,
            )
            if context.workflow_mode is WorkflowMode.DEEP_RESEARCH:
                agent = DeepResearchAgent(use_deeper_tool=True, retrieval_provider=provider, run_id=context.run_id)
                agents.append(agent)
                return DeepResearchDriver(context, agent, events=events)
            worker = WorkerCoordinator(
                retrieval_provider=provider,
                max_parallel_workers=context.budget_limits.max_concurrency,
            )
            bundle = MultiAgentFactory.create_default_bundle(retrieval_provider=provider, worker=worker)
            return PlanExecuteReportDriver(context, bundle.orchestrator, events=events)

        runtime = HarnessRuntime(
            run_repository=self.runs, message_repository=self.messages,
            event_repository=self.events, checkpoint_repository=CheckpointRepository(self.database),
            evidence_repository=EvidenceRepository(self.database), contract_repository=ContractRepository(self.database),
            trajectory_repository=PlanTaskToolRepository(self.database), workflow_factory=workflow_factory,
            artifact_store=self.artifact_store, artifact_repository=ArtifactRepository(self.database),
            event_bus=self.event_bus,
            context_builder=self.context_builder, memory_extractor=self.memory_extractor, skill_distiller=None,
            prefix_tracker=prefix_tracker,
        )
        try:
            return await runtime.execute_run(run_id)
        finally:
            for agent in agents:
                if hasattr(agent, "close"):
                    agent.close()
            run = await self.runs.get(run_id)
            config = json.loads(run.config_snapshot_json or "{}") if run else {}
            if not config.get("evaluation_run") and run and run.status not in {"paused", "pausing"}:
                await self.skill_learning.enqueue_for_run(run_id)
                snapshot = json.loads(run.model_snapshot_json or "{}") if run else {}
                selected = snapshot.get("skill") if isinstance(snapshot, dict) else None
                if isinstance(selected, dict) and selected.get("status") == "canary":
                    checks = await ContractRepository(self.database).list_for_run(run_id)
                    safety_violation = any(item.kind == "source_match" and item.passed != 1 for item in checks)
                    await self.skill_promotion.observe_run(
                        name=str(selected.get("name")), version=str(selected.get("version")),
                        succeeded=bool(run and run.status == "completed"), safety_violation=safety_violation,
                    )

    async def run_evaluation_case(self, *, query: str, source_mode: str, workflow_mode: str, forced_skill=None) -> dict[str, Any]:
        """Execute one isolated real Harness Run for paired Skill evaluation."""
        session = await self.sessions.create(SessionCreate(title="[Skill Eval]"))
        message, run, _ = await self.runs.create_for_user_message(
            MessageCreate(session_id=session.session_id, role="user", content=query, client_message_id=None, metadata={"evaluation": True}),
            RunCreate(
                session_id=session.session_id, trigger_message_id="assigned-atomically",
                source_mode=SourceMode(source_mode), workflow_mode=WorkflowMode(workflow_mode),
                config_snapshot={
                    "evaluation_run": True, "disable_skills": forced_skill is None,
                    "forced_skill": forced_skill, "min_evidence": 1,
                    "report_type": "brief", "deep_research_max_iterations": 1,
                    "schema_version": 1,
                },
                budget=settings.HARNESS_BUDGETS,
            ),
        )
        await self.runs.update_model_snapshot(run.run_id, {
            "llm_model": settings.OPENAI_LLM_MODEL,
            "learning_review_model": settings.LEARNING_REVIEW_MODEL,
            "evaluation_arm": "treatment" if forced_skill else "control",
        })
        await self._execute(run.run_id)
        finished = await self.runs.get(run.run_id)
        checks = await ContractRepository(self.database).list_for_run(run.run_id)
        usage = json.loads(finished.usage_json or "{}") if finished else {}
        check_map = {item.kind: bool(item.passed) for item in checks}
        limits = usage.get("limits", {}) if isinstance(usage, dict) else {}
        used = usage.get("usage", usage) if isinstance(usage, dict) else {}
        return {
            "run_id": run.run_id, "completed": bool(finished and finished.status == "completed"),
            "status": finished.status if finished else "missing",
            "citation_integrity": float(check_map.get("citation_integrity", False)),
            "claim_support": float(check_map.get("claim_support", False)),
            "tool_policy_violations": 0,
            "source_leakage": 0 if check_map.get("source_match", False) else 1,
            "safety_passed": bool(check_map.get("source_match", False)),
            "llm_tokens": int(used.get("llm_tokens", 0) or 0),
            "wall_time_seconds": float(used.get("elapsed_seconds", 0) or 0),
            "checks": check_map,
        }

    async def startup_recovery(self, *, auto_resume: bool = AUTO_RESUME_RUNS) -> list[str]:
        run_ids = await RecoveryManager(self.runs).scan(auto_resume=auto_resume)
        for run_id in run_ids:
            self.schedule(run_id)
        await self.skill_learning.recover()
        await self.commands.recover()
        return run_ids

    async def cancel(self, run_id: str) -> bool:
        requested = await self.runs.request_cancel(run_id)
        if not requested:
            return False
        await self.commands.cancel_all(run_id)
        mark_run_cancelled(run_id)
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
        else:
            run = await self.runs.get(run_id)
            if run is not None:
                await self.runs.update_status(
                    run_id, status="cancelled", current_stage="cancelled",
                    usage=json.loads(run.usage_json or "{}"),
                )
                await self.event_bus.publish(run_id, "run.cancelled", stage="cancelled", payload={"status": "cancelled"})
        return True

    async def pause(self, run_id: str) -> bool:
        requested = await self.runs.request_pause(run_id)
        if not requested:
            return False
        await self.event_bus.publish(
            run_id, "run.pause_requested", stage="pausing", payload={"status": "pausing"},
        )
        task = self._tasks.get(run_id)
        if task is None or task.done():
            if await self.runs.finalize_pause(run_id):
                run = await self.runs.get(run_id)
                await self.event_bus.publish(
                    run_id, "run.paused", stage="paused",
                    payload={"status": "paused", "resume_from_status": run.current_stage if run else "queued"},
                )
        return True

    async def resume(self, run_id: str, *, clarification: str | None = None) -> bool:
        if await self.commands.pending(run_id):
            return False
        before = await self.runs.get(run_id)
        pending_pause = bool(
            before and json.loads(before.config_snapshot_json or "{}").get("pause_requested")
        )
        cancelling_pending_pause = bool(
            pending_pause and before and before.status != "paused"
        )
        resumed = await self.runs.resume(run_id, clarification=clarification)
        if resumed:
            current = await self.runs.get(run_id)
            await self.event_bus.publish(
                run_id, "run.clarification_received" if clarification else "run.resume_requested",
                stage=current.current_stage if current else "queued",
                payload={
                    "has_clarification": bool(clarification),
                    "status": current.status if current else "queued",
                    "cancelled_pending_pause": cancelling_pending_pause,
                },
            )
            if cancelling_pending_pause:
                return True
            active = self._tasks.get(run_id)
            if active is not None and not active.done():
                loop = asyncio.get_running_loop()
                active.add_done_callback(lambda _task: loop.call_soon(self.schedule, run_id))
            else:
                self.schedule(run_id)
        return resumed

    async def shutdown(self) -> None:
        for task in self.commands.workers.values():
            task.cancel()
        await asyncio.gather(*self.commands.workers.values(), return_exceptions=True)
        await self.skill_learning.shutdown()
        active = list(self._tasks.values())
        if not active:
            return
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
