import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";

type Task = { task_id: string; description: string; status: string; task_revision?: number };
export type EditState = { plan: { version: number; source_mode: string; task_graph: { nodes: Task[] } } | null; command: {
  request_id: string; status: string; explanation?: string; preview_hash?: string;
  impact?: { reused: string[]; invalidated: string[]; skipped: string[]; pending: string[] };
} | null };

export function PausedRunEditor({runId, onChanged, onError}: {runId: string; onChanged: () => void; onError: (error: unknown) => void}) {
  const [target, setTarget] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [clarification, setClarification] = useState("");
  const query = useQuery({queryKey: ["edit-state", runId], queryFn: () => api.editState(runId), refetchInterval: 1000});
  const command = query.data?.command;
  const notified = useRef("");
  useEffect(() => {
    if (command?.status === "applied" && notified.current !== command.request_id) {
      notified.current = command.request_id;
      onChanged();
    }
  }, [command?.status, command?.request_id, onChanged]);
  const pending = command && !["applied", "cancelled", "failed"].includes(command.status);
  async function operate(type: string, taskId?: string, changes?: Record<string, unknown>) {
    setBusy(true);
    try {
      await api.editCommand(runId, {request_id: crypto.randomUUID(), preview_only: true,
        command: {intent: "modify", operations: [{type, task_id: taskId, changes: changes ?? {}}]}});
      setTarget(null); await query.refetch();
    } catch (error) { onError(error); } finally { setBusy(false); }
  }
  async function finish(apply: boolean) {
    if (!command) return;
    setBusy(true);
    try {
      if (apply) await api.applyEdit(runId, command.request_id, command.preview_hash);
      else await api.cancelEdit(runId, command.request_id);
      await query.refetch(); onChanged();
    } catch (error) { onError(error); } finally { setBusy(false); }
  }
  const statuses: Record<string, string> = {received: "已接收修改", waiting_for_pause: "等待当前任务结束", interpreting: "正在理解修改",
    ready: "请检查修改影响", needs_clarification: "需要补充说明", applied: "修改已应用", cancelled: "修改已撤销"};
  return <details className="runtime-skill-banner" style={{maxHeight: "35vh", overflowY: "auto"}} open>
    <summary>局部修改 · 计划版本 {query.data?.plan?.version ?? "—"}</summary>
    {query.error && <p role="alert">无法读取修改状态</p>}
    {command && <div aria-live="polite"><p>{statuses[command.status] ?? command.status} {command.explanation}</p>
      {command.impact && <p>复用 {command.impact.reused.length} 项 · 失效 {command.impact.invalidated.length} 项 · 跳过 {command.impact.skipped.length} 项 · 待执行 {command.impact.pending.length} 项</p>}
      {pending && <div>{command.status === "ready" && <button disabled={busy} onClick={() => finish(true)}>应用修改（保持暂停）</button>}
        {command.status === "needs_clarification" && <div><input aria-label="补充修改说明" value={clarification} onChange={e => setClarification(e.target.value)} />
          <button disabled={busy || !clarification.trim()} onClick={async () => {
            setBusy(true);
            try {await api.clarifyEdit(runId, command.request_id, clarification); setClarification(""); await query.refetch();}
            catch (error) {onError(error);} finally {setBusy(false);}
          }}>提交补充</button></div>}
        <button disabled={busy} onClick={() => finish(false)}>撤销此修改</button></div>}
    </div>}
    {query.data?.plan?.task_graph.nodes.map(task => <div key={task.task_id} style={{padding: "6px 0"}}>
      <span>{task.description} · {({completed: "已完成，可复用", pending: "待执行", skipped: "已跳过", failed: "失败", running: "执行中"} as Record<string, string>)[task.status] ?? task.status}</span>
      <button disabled={busy || Boolean(pending)} onClick={() => {setTarget(task.task_id); setDescription(task.description);}}>修改</button>
      <button disabled={busy || Boolean(pending) || task.status === "skipped"} onClick={() => operate("skip_task", task.task_id)}>跳过</button>
      <button disabled={busy || Boolean(pending)} onClick={() => operate("rerun_task", task.task_id)}>重跑</button>
    </div>)}
    <button disabled={busy || Boolean(pending)} onClick={() => {setTarget("new"); setDescription("");}}>添加任务</button>
    {target && <div><textarea aria-label="任务描述" value={description} onChange={e => setDescription(e.target.value)} />
      <button disabled={busy || !description.trim()} onClick={() => operate(target === "new" ? "add_task" : "update_task", target === "new" ? undefined : target,
        target === "new" ? {description, task_type: query.data?.plan?.source_mode === "web" ? "web_search" : "hybrid_search"} : {description})}>预览修改</button>
      <button onClick={() => setTarget(null)}>关闭编辑</button></div>}
    <p>也可以输入“跳过某项，其他继续”或“只把报告改成中文”。依赖有冲突时会保持暂停。</p>
  </details>;
}
