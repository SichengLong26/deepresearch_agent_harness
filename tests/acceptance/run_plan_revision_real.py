"""Opt-in live provider acceptance; creates one isolated Run with bounded budget.

Run with PYTHONPATH=src;. .venv/Scripts/python tests/acceptance/run_plan_revision_real.py
"""
import json
import time
import uuid
import os
from pathlib import Path

os.environ["REPORT_RESERVED_TOKENS"] = "6000"
os.environ["VERIFICATION_RESERVED_TOKENS"] = "1500"
os.environ["TAVILY_MAX_RESULTS"] = "2"
os.environ["TAVILY_SEARCH_DEPTH"] = "basic"
os.environ["REPORT_MAX_SECTIONS"] = "3"
os.environ["REPORT_SECTION_EVIDENCE_BUDGET"] = "3000"

from fastapi.testclient import TestClient
from backend.app.main import create_app
from backend.app.services.chat_service import HARNESS_BUDGETS


def main():
    root = Path("data/acceptance/partial-resume") / uuid.uuid4().hex[:8]
    root.mkdir(parents=True)
    HARNESS_BUDGETS.update(max_llm_tokens=200000, max_tool_calls=12, max_concurrency=1, wall_time_seconds=900)
    app = create_app(database_url=f"sqlite+aiosqlite:///{(root / 'app.db').as_posix()}",
        artifact_root=root / "artifacts", skills_root=root / "skills", auto_resume=False)
    app.state.run_service.skill_learning.enabled = False
    with TestClient(app) as client:
        session = client.post("/api/v1/sessions", json={"title": "局部修改真实链路验收"}).json()["session_id"]
        response = client.post(f"/api/v1/sessions/{session}/messages", json={
            "client_message_id": str(uuid.uuid4()), "content": "请简短研究 SQLite。规划三个相互独立、无依赖的联网检索任务：1. 官方对 SQLite 的定义；2. WAL 模式的基本用途；3. SQLite 官方命令行工具。每项只做一次搜索，最终用中文给出约300字带引用说明。",
            "source_mode": "web", "workflow_mode": "plan_execute_report", "report_type": "brief"})
        response.raise_for_status()
        run_id = response.json()["run_id"]
        print(json.dumps({"run_id": run_id, "database": str(root / "app.db")}), flush=True)

        def events():
            return client.portal.call(app.state.run_service.events.list_after, run_id)

        deadline = time.monotonic() + 900
        def wait_for(predicate):
            while time.monotonic() < deadline:
                value = predicate()
                if value:
                    return value
                current = client.get(f"/api/v1/runs/{run_id}").json()
                if current["status"] in {"failed", "cancelled", "budget_exhausted"}:
                    raise AssertionError(current)
                time.sleep(0.5)
            raise AssertionError("真实链路验收超时")

        try:
            wait_for(lambda: any(e.event_type == "task.started" for e in events()))
            client.post(f"/api/v1/runs/{run_id}/pause").raise_for_status()
            wait_for(lambda: client.get(f"/api/v1/runs/{run_id}").json()["status"] == "paused")
            before = client.get(f"/api/v1/runs/{run_id}/edit-state").json()["plan"]
            completed = [n["task_id"] for n in before["task_graph"]["nodes"] if n["status"] == "completed"]
            remaining = [n for n in before["task_graph"]["nodes"] if n["status"] == "pending"]
            assert completed and remaining, before
            target = remaining[-1]
            command_id = str(uuid.uuid4())
            response = client.post(f"/api/v1/sessions/{session}/messages", json={
                "client_message_id": command_id, "content": f"跳过这个不执行了：{target['description']}。其他按原计划继续。",
                "target_run_id": run_id, "source_mode": "web", "workflow_mode": "plan_execute_report"})
            response.raise_for_status()
            assert response.json()["run_id"] == run_id
            command = wait_for(lambda: (r if r["status"] in {"applied", "needs_clarification", "failed"} else None)
                if (r := client.get(f"/api/v1/runs/{run_id}/commands/{command_id}").json()) else None)
            print(json.dumps({"command": command}, ensure_ascii=False), flush=True)
            assert command["status"] == "applied", command
            wait_for(lambda: client.get(f"/api/v1/runs/{run_id}").json()["status"] in {"completed", "failed", "budget_exhausted"})
            final = client.get(f"/api/v1/runs/{run_id}").json()
            all_events = events()
            starts = [json.loads(e.payload_json).get("task_id") for e in all_events if e.event_type == "task.started"]
            assert all(starts.count(task_id) == 1 for task_id in completed), starts
            assert target["task_id"] not in starts, starts
            assert sum(e.event_type == "plan.created" for e in all_events) == 1
            assert not any(e.event_type == "plan.replanning" for e in all_events)
            assert final["status"] == "completed", final
            report = client.get(f"/api/v1/runs/{run_id}/report").json()
            result = {"run_id": run_id, "status": final["status"], "reused": completed, "skipped": target["task_id"],
                "task_started_ids": starts, "command": command, "report_characters": len(report.get("content") or ""), "usage": final["usage"]}
            (root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        finally:
            final = client.get(f"/api/v1/runs/{run_id}").json()
            if final["status"] not in {"completed", "cancelled", "failed", "budget_exhausted"}:
                client.post(f"/api/v1/runs/{run_id}/cancel")


if __name__ == "__main__":
    main()
