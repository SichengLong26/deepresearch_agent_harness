import { Report, RunEvent } from "../types/api";

/** 迭代中的一次搜索 */
export interface IterationQuery { query: string; result_count: number; found_useful: boolean }
/** 单轮迭代聚合（由 iteration.completed 事件合并而来） */
export interface IterationEntry {
  index: number;
  queries: IterationQuery[];
  total_results: number;
  info_snippets: string[];
  answer_char_count?: number;
}
/** 工具调用条目（tool.completed 增强 payload） */
export interface ToolEntry { tool_name: string; query?: string; result_count?: number; task_id?: string; failed?: boolean; output_preview?: string }
/** 计划任务（plan.created/revised 增强 payload） */
export interface PlanTask { task_id: string; task_type: string; description: string }
export interface RecoveryInfo {
  action: "repair_report" | "replan" | "fail" | "complete";
  attempt?: number;
  maxAttempts?: number;
  attemptsExhausted: boolean;
}

export interface StageFeed {
  iterations: IterationEntry[];
  /** 尚无 iteration.completed 收尾的最新迭代号（进行中） */
  runningIteration: number | null;
  /** 进行中迭代的实时搜索行（agent.progress kind=search） */
  liveSearches: Array<{ iteration_index: number; query: string; result_count?: number; found_useful?: boolean }>;
  planTasks: PlanTask[];
  tools: ToolEntry[];
  contextCount: number;
  taskCount: number;
  taskStartedCount: number;
  /** 冻结 Memory / 历史召回 / 复用消息（context.completed payload） */
  contextInfo?: { memories: number; usedMessages: number; historicalRecall: number; tokens: number };
  reportChars?: number;
  verifyFailures: Array<{ kind: string; message?: string }>;
  verification: Report["verification"];
  recovery?: RecoveryInfo;
}

const numberOr = (value: unknown, fallback = 0): number => (typeof value === "number" ? value : fallback);
const stringOr = (value: unknown, fallback = ""): string => (typeof value === "string" ? value : fallback);

function parseIterations(events: RunEvent[]): IterationEntry[] {
  const byIndex = new Map<number, IterationEntry>();
  events
    .filter((event) => event.event_type === "iteration.completed")
    .forEach((event) => {
      const kind = stringOr(event.kind);
      if (kind === "answer") {
        // 最终答案并入最后一轮真实迭代（answer 的 index 为已完成轮数，会多出一位）
        const sorted = [...byIndex.values()].sort((a, b) => a.index - b.index);
        const last = sorted.length > 0 ? sorted[sorted.length - 1] : undefined;
        const entry = last ?? { index: numberOr(event.iteration_index), queries: [], total_results: 0, info_snippets: [] };
        entry.answer_char_count = numberOr(event.answer_char_count);
        byIndex.set(entry.index, entry);
        return;
      }
      const index = numberOr(event.iteration_index);
      const entry = byIndex.get(index) ?? { index, queries: [], total_results: 0, info_snippets: [] };
      entry.queries = Array.isArray(event.queries)
        ? (event.queries as IterationQuery[]).map((q) => ({
            query: stringOr(q.query),
            result_count: numberOr(q.result_count),
            found_useful: Boolean(q.found_useful),
          }))
        : entry.queries;
      entry.total_results = numberOr(event.total_results);
      if (Array.isArray(event.info_snippets)) {
        entry.info_snippets = (event.info_snippets as unknown[]).slice(0, 3).map((snippet) => String(snippet));
      }
      byIndex.set(index, entry);
    });
  return [...byIndex.values()].sort((a, b) => a.index - b.index);
}

function parsePlanTasks(events: RunEvent[]): PlanTask[] {
  let tasks: PlanTask[] = [];
  events
    .filter((event) => event.event_type === "plan.created" || event.event_type === "plan.revised" || event.event_type === "plan.revision_applied")
    .forEach((event) => {
      if (Array.isArray(event.tasks)) {
        tasks = (event.tasks as PlanTask[]).map((task) => ({
          task_id: stringOr(task.task_id),
          task_type: stringOr(task.task_type),
          description: stringOr(task.description),
        }));
      }
    });
  return tasks;
}

function parseTools(events: RunEvent[]): ToolEntry[] {
  const seen = new Set<string>();
  return events
    .filter((event) => {
      if (event.event_type !== "tool.completed" && event.event_type !== "tool.failed") return false;
      const key = stringOr(event.tool_call_id, `${event.task_id}:${event.tool_name}:${event.query}`);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    })
    .map((event) => ({
      tool_name: stringOr(event.tool_name),
      query: typeof event.query === "string" ? event.query : undefined,
      result_count: typeof event.result_count === "number" ? event.result_count : undefined,
      task_id: typeof event.task_id === "string" ? event.task_id : undefined,
      failed: event.event_type === "tool.failed",
      output_preview: typeof event.output_preview === "string" ? event.output_preview : undefined,
    }));
}

/** 从事件流派生出画板各阶段卡片所需的全部中间数据 */
export function deriveStageFeed(events: RunEvent[], report?: Report | null): StageFeed {
  const iterations = parseIterations(events);
  const planTasks = parsePlanTasks(events);
  const currentPlanTaskIds = new Set(planTasks.map((task) => task.task_id));
  const revisionEvent = [...events].reverse().find(event => event.event_type === "plan.revision_applied");
  const reused = Array.isArray(revisionEvent?.reused) ? revisionEvent.reused.map(String) : [];
  const revisionEventId = numberOr(revisionEvent?.event_id, Number.MAX_SAFE_INTEGER);
  const isCurrentPlanTask = (event: RunEvent) => (currentPlanTaskIds.size === 0 || currentPlanTaskIds.has(stringOr(event.task_id)))
    && (!revisionEvent || numberOr(event.event_id, -1) > revisionEventId || reused.includes(stringOr(event.task_id)));
  // 完成态：含 answer 事件自己的 index（其值为已完成轮数，会多出一位）
  const completedIndexes = new Set(
    events.filter((event) => event.event_type === "iteration.completed").map((event) => numberOr(event.iteration_index))
  );
  const progressTicks = events.filter((event) => event.event_type === "agent.progress");
  const iterationTicks = progressTicks
    .filter((event) => stringOr(event.kind) === "iteration")
    .map((event) => numberOr(event.iteration_index));
  const runningIteration = [...iterationTicks].reverse().find((index) => !completedIndexes.has(index)) ?? null;

  const liveSearches = progressTicks
    .filter((event) => stringOr(event.kind) === "search")
    .map((event) => ({
      iteration_index: numberOr(event.iteration_index),
      query: stringOr(event.query),
      result_count: typeof event.result_count === "number" ? event.result_count : undefined,
      found_useful: typeof event.found_useful === "boolean" ? event.found_useful : undefined,
    }));

  let contextInfo: StageFeed["contextInfo"];
  const contextEvent = events.find((event) => event.event_type === "context.completed");
  if (contextEvent) {
    contextInfo = {
      memories: numberOr(contextEvent.curated_memory_count),
      usedMessages: Array.isArray(contextEvent.used_message_ids) ? contextEvent.used_message_ids.length : 0,
      historicalRecall: numberOr(contextEvent.historical_recall_count),
      tokens: numberOr(contextEvent.context_tokens),
    };
  }

  const verifyEvent = [...events].reverse().find((event) => event.event_type === "verification.completed");
  const verifyFailures = Array.isArray(verifyEvent?.failures)
    ? (verifyEvent!.failures as unknown[]).map((failure) => (
        typeof failure === "string"
          ? { kind: failure }
          : { kind: stringOr((failure as { kind?: unknown }).kind), message: typeof (failure as { message?: unknown }).message === "string" ? String((failure as { message?: unknown }).message) : undefined }
      ))
    : [];
  const action = stringOr(verifyEvent?.recovery_action);
  const rawAttempt = typeof verifyEvent?.attempt === "number" ? verifyEvent.attempt : undefined;
  const maxAttempts = typeof verifyEvent?.max_attempts === "number" ? verifyEvent.max_attempts : undefined;
  const recovery = ["repair_report", "replan", "fail", "complete"].includes(action)
    ? {
        action: action as RecoveryInfo["action"],
        attempt: rawAttempt != null && maxAttempts != null ? Math.min(rawAttempt, maxAttempts) : rawAttempt,
        maxAttempts,
        attemptsExhausted: verifyEvent?.attempts_exhausted === true || (rawAttempt != null && maxAttempts != null && rawAttempt > maxAttempts),
      }
    : undefined;

  const reportEvent = [...events].reverse().find((event) => event.event_type === "report.completed");

  return {
    iterations,
    runningIteration,
    liveSearches,
    planTasks,
    tools: parseTools(events),
    contextCount: new Set(events.filter((event) => event.event_type === "evidence.added").map((event) => stringOr(event.evidence_id))).size,
    taskCount: new Set(events.filter((event) => event.event_type === "task.completed" && isCurrentPlanTask(event)).map((event) => stringOr(event.task_id))).size,
    taskStartedCount: new Set(events.filter((event) => event.event_type === "task.started" && isCurrentPlanTask(event)).map((event) => stringOr(event.task_id))).size,
    contextInfo,
    reportChars: typeof reportEvent?.characters === "number" ? reportEvent.characters : undefined,
    verifyFailures,
    verification: report?.verification ?? [],
    recovery,
  };
}
