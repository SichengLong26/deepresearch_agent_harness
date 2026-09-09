"""Checkpoint serialization, integrity validation and safe restoration."""

from __future__ import annotations

import json

from .run_context import RunContext
from deepresearch_agent.persistence.repositories import CheckpointRepository


class CheckpointCorrupt(RuntimeError):
    pass


class CheckpointManager:
    def __init__(self, repository: CheckpointRepository):
        self.repository = repository

    async def save(self, context: RunContext, stage: str):
        checkpoint = await self.repository.save(context.run_id, stage, context.model_dump(mode="json"))
        context.checkpoint_version = checkpoint.version
        return checkpoint

    async def restore(self, run_id: str) -> RunContext | None:
        checkpoint = await self.repository.latest(run_id)
        if checkpoint is None:
            return None
        if not self.repository.verify(checkpoint):
            raise CheckpointCorrupt(f"Run {run_id} 的 checkpoint hash 校验失败")
        payload = json.loads(checkpoint.state_json)
        from .versioning import CHECKPOINT_SCHEMA_VERSION
        if payload.get("schema_version", checkpoint.schema_version) > CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointCorrupt("检查点来自更高版本，不能安全恢复")
        payload.pop("schema_version", None)
        context = RunContext.model_validate(payload)
        context.checkpoint_version = checkpoint.version
        return context

