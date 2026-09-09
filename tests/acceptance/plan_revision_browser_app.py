"""Isolated, seeded UI fixture; no external model calls for card editing."""
from contextlib import asynccontextmanager
from pathlib import Path
import uuid

from fastapi.middleware.cors import CORSMiddleware
from backend.app.main import create_app
from backend.app.schemas import SessionCreate, MessageCreate, RunCreate
from deepresearch_agent.persistence.repositories import SessionRepository, RunRepository, CheckpointRepository, PlanTaskToolRepository
from tests.harness.test_plan_revision import snapshot
from tests.api.test_api_phase4 import ApiFakeDriver

root = Path(".pytest-tmp") / ("partial-ui-" + uuid.uuid4().hex[:8])
app = create_app(database_url=f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}",
    artifact_root=root / "artifacts", skills_root=root / "skills", workflow_factory=ApiFakeDriver, auto_resume=False)
app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:5178"], allow_methods=["*"], allow_headers=["*"])
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def lifespan(application):
    async with original_lifespan(application):
        database = app.state.database
        session = await SessionRepository(database).create(SessionCreate(title="局部修改页面验收"))
        message, run, _ = await RunRepository(database).create_for_user_message(
            MessageCreate(session_id=session.session_id, role="user", content="页面验收：调查 SQLite 定义、WAL 与 CLI", client_message_id="seed"),
            RunCreate(session_id=session.session_id, trigger_message_id="atomic", source_mode="graphrag", workflow_mode="plan_execute_report"))
        context = snapshot(run.run_id, session.session_id, message.message_id)
        plan = context.workflow_state["state"]["plan"]
        for node, description in zip(plan["task_graph"]["nodes"], ["SQLite 官方定义", "调查 WAL 模式", "调查 CLI 工具"]):
            node["description"] = description
        await PlanTaskToolRepository(database).save_plan(run_id=run.run_id, plan_id=plan["plan_id"], version=1,
            status="approved", plan=plan, tasks=plan["task_graph"]["nodes"], source_mode="graphrag")
        await CheckpointRepository(database).save(run.run_id, "paused", context.model_dump(mode="json"))
        await RunRepository(database).update_status(run.run_id, status="paused", current_stage="executing", usage={})
        await app.state.run_service.event_bus.publish(run.run_id, "plan.created", stage="planning", payload={"tasks": plan["task_graph"]["nodes"]})
        yield


app.router.lifespan_context = lifespan
