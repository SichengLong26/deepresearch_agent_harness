import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.app.schemas import MessageCreate, RunCreate, SessionCreate
from backend.app.services.run_command_service import RunCommandService
from deepresearch_agent.agents.multi_agent.core.plan_spec import PlanSpec, ProblemStatement, TaskGraph, TaskNode
from deepresearch_agent.agents.multi_agent.core.state import PlanExecuteState
from deepresearch_agent.harness.plan_revision import EditCommand, revise
from deepresearch_agent.harness.run_context import RunContext
from deepresearch_agent.persistence import Database
from deepresearch_agent.persistence.repositories import CheckpointRepository, RunRepository, SessionRepository


def snapshot(run_id="run", session_id="session", message_id="message"):
    plan = PlanSpec(problem_statement=ProblemStatement(original_query="research"), task_graph=TaskGraph(nodes=[
        TaskNode(task_id="A", task_type="custom", description="A", status="completed"),
        TaskNode(task_id="B", task_type="custom", description="B", depends_on=["A"]),
        TaskNode(task_id="C", task_type="custom", description="C"),
    ]))
    state = PlanExecuteState(input="research", plan=plan)
    state.execution_context.completed_task_ids = ["A"]
    state.execution_context.intermediate_results = {"A": "saved"}
    return RunContext(run_id=run_id, session_id=session_id, trigger_message_id=message_id,
        source_mode="graphrag", workflow_mode="plan_execute_report", status="paused", original_query="research",
        plan_version=1, resume_from_status="executing", workflow_state={"state": state.model_dump(mode="json"),
        "planner_result": {"plan_spec": plan.model_dump(mode="json"), "executor_signal": plan.to_execution_signal().model_dump(mode="json")}})


def edit(kind, task_id=None, changes=None, **kwargs):
    return EditCommand(intent="modify", operations=[{"type": kind, "task_id": task_id, "changes": changes or {}, **kwargs}])


def test_edit_preserves_independent_result_and_refreshes_signal():
    original = snapshot()
    result, impact = revise(original, edit("update_task", "C", {"description": "new C"}))
    assert result.run_id == original.run_id
    assert result.plan_version == 2
    assert impact["reused"] == ["A"]
    assert result.workflow_state["state"]["execution_context"]["intermediate_results"] == {"A": "saved"}
    assert result.workflow_state["planner_result"]["executor_signal"]["tasks"][2]["description"] == "new C"
    assert original.plan_version == 1


def test_edit_invalidates_transitive_dependencies():
    result, impact = revise(snapshot(), edit("update_task", "A", {"description": "new A"}))
    assert impact["invalidated"] == ["A", "B"]
    assert result.workflow_state["state"]["execution_context"]["completed_task_ids"] == []
    assert result.workflow_state["state"]["execution_context"]["intermediate_results"] == {}


def test_skip_dependency_requires_explicit_cascade():
    with pytest.raises(ValueError, match="阻塞"):
        revise(snapshot(), edit("skip_task", "A"))
    result, impact = revise(snapshot(), edit("skip_task", "A", dependents_policy="cascade_skip"))
    assert impact["skipped"] == ["A", "B"]
    assert impact["pending"] == ["C"]
    criteria = result.workflow_state["state"]["plan"]["acceptance_criteria"]["completion_conditions"]
    assert "不参与执行、报告覆盖或完成度判定" in criteria[0]


def test_cycle_and_forbidden_fields_rejected():
    with pytest.raises(ValueError):
        revise(snapshot(), edit("update_task", "A", {"depends_on": ["B"]}))
    with pytest.raises(ValueError):
        revise(snapshot(), edit("update_task", "A", {"status": "completed"}))


def test_report_only_edit_reuses_completed_research():
    context = snapshot()
    for task in context.workflow_state["state"]["plan"]["task_graph"]["nodes"]:
        task["status"] = "completed"
    result, impact = revise(context, edit("update_report_requirements", changes={"language": "中文"}))
    assert impact["reused"] == ["A", "B", "C"]
    assert result.resume_cursor.value == "reporting"


@pytest.mark.parametrize("mode", ["sequential", "parallel"])
def test_pause_edit_resume_calls_only_remaining_tasks(mode):
    from deepresearch_agent.agents.multi_agent.core.execution_record import ExecutionRecord, ExecutionMetadata
    from deepresearch_agent.agents.multi_agent.executor.base_executor import BaseExecutor, TaskExecutionResult
    from deepresearch_agent.agents.multi_agent.executor.worker_coordinator import WorkerCoordinator

    calls = []
    class Worker(BaseExecutor):
        def can_handle(self, task_type):
            return True

        def execute_task(self, task, state, signal):
            calls.append((task.task_id, task.description))
            state.plan.update_task_status(task.task_id, "completed")
            state.execution_context.completed_task_ids.append(task.task_id)
            state.execution_context.intermediate_results[task.task_id] = task.description
            record = ExecutionRecord(task_id=task.task_id, session_id=state.session_id, worker_type="test",
                metadata=ExecutionMetadata(worker_type="test"))
            return TaskExecutionResult(record=record, success=True)

    context = snapshot()
    raw = context.workflow_state["state"]
    raw["plan"]["task_graph"]["nodes"][0]["status"] = "pending"
    raw["execution_context"]["completed_task_ids"] = []
    state = PlanExecuteState.model_validate(raw)
    state.plan = PlanSpec.model_validate(raw["plan"])
    worker = WorkerCoordinator(executors=[Worker()], execution_mode=mode, max_parallel_workers=1)
    checkpoints = []
    worker.execute_plan(state, state.plan.to_execution_signal(), stop_predicate=lambda: len(calls) >= 1,
        progress_callback=lambda kind, task, record: checkpoints.append(state.model_dump(mode="json")) if record else None)
    assert calls == [("A", "A")]
    assert len(checkpoints[-1]["execution_records"]) == 1
    context.workflow_state["state"] = checkpoints[-1]
    candidate, impact = revise(context, edit("update_task", "B", {"description": "new B"}))
    resumed = PlanExecuteState.model_validate(candidate.workflow_state["state"])
    resumed.plan = PlanSpec.model_validate(candidate.workflow_state["state"]["plan"])
    worker.execute_plan(resumed, resumed.plan.to_execution_signal())
    assert calls == [("A", "A"), ("B", "new B"), ("C", "C")]
    assert impact["reused"] == ["A"]


@pytest.mark.asyncio
async def test_atomic_revision_duplicate_and_restart(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'revision.db').as_posix()}")
    await database.create_schema()
    sessions, runs = SessionRepository(database), RunRepository(database)
    session = await sessions.create(SessionCreate(title="revision"))
    message, run, _ = await runs.create_for_user_message(
        MessageCreate(session_id=session.session_id, role="user", content="research", client_message_id="first"),
        RunCreate(session_id=session.session_id, trigger_message_id="atomic", source_mode="graphrag", workflow_mode="plan_execute_report"))
    context = snapshot(run.run_id, session.session_id, message.message_id)
    await CheckpointRepository(database).save(run.run_id, "paused", context.model_dump(mode="json"))
    await runs.update_status(run.run_id, status="paused", current_stage="executing", usage={})
    # Real repositories/transaction, no research or provider calls needed for an edit.
    service = SimpleNamespace(database=database, runs=runs)
    commands = RunCommandService(service)
    payload = {"command": edit("update_task", "C", {"description": "new"}).model_dump()}
    await commands.submit(run.run_id, "edit-1", payload)
    await commands.workers[(run.run_id, "edit-1")]
    result = await commands.get(run.run_id, "edit-1")
    assert result["status"] == "applied", result
    latest = await CheckpointRepository(database).latest(run.run_id)
    assert CheckpointRepository.verify(latest)
    assert json.loads(latest.state_json)["resume_cursor"] == "executing"
    again = await commands.submit(run.run_id, "edit-1", payload)
    assert again == result
    await database.close()
    reopened = Database(f"sqlite+aiosqlite:///{(tmp_path / 'revision.db').as_posix()}")
    restored = await CheckpointRepository(reopened).latest(run.run_id)
    assert restored.version == latest.version
    assert json.loads(restored.state_json)["workflow_state"]["state"]["execution_context"]["completed_task_ids"] == ["A"]
    await reopened.close()
