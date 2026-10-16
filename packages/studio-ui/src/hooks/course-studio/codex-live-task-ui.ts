import type {
  AgentActivityEvent,
  RealtimeToolCallResponse,
  RealtimeToolName,
} from "../../types";

export type RealtimeToolStatusUpdate = {
  lessonId: string;
  turnId: string;
  delegationId?: string;
  label: string;
  status: "pending" | "waiting" | "queued" | "running" | "completed" | "cancelled" | "error";
  activity?: AgentActivityEvent;
};

export type CodexLiveTaskAction = "supplement" | "replace" | "queue" | "dismiss";

export type CodexLivePendingTask = {
  delegationId: string;
  text: string;
};

export type CodexLiveRunStatus = "waiting" | "queued" | "running" | "completed" | "cancelled" | "error";

export type CodexLiveRunState = {
  workflowRunId: string;
  turnId: string;
  delegationId?: string;
  status: CodexLiveRunStatus;
  route?: "ordinary_chat" | "learning_need" | "unclear";
  label: string;
  documentResultSucceeded: boolean;
  speechStatus: "not_requested" | "failed";
  visibleAsTask: boolean;
};

export type CodexLiveTaskState = {
  runsByWorkflowId: Record<string, CodexLiveRunState>;
  pendingTasks: CodexLivePendingTask[];
};

export type CodexLiveTaskSummary = {
  runningCount: number;
  queuedCount: number;
  pendingCount: number;
};

export function createCodexLiveTaskState(): CodexLiveTaskState {
  return {
    runsByWorkflowId: {},
    pendingTasks: [],
  };
}

export function codexLiveTaskSummary(state: CodexLiveTaskState): CodexLiveTaskSummary {
  return Object.values(state.runsByWorkflowId).reduce<CodexLiveTaskSummary>(
    (summary, run) => {
      if (!run.visibleAsTask) {
        return summary;
      }
      if (run.status === "running") {
        summary.runningCount += 1;
      } else if (run.status === "queued") {
        summary.queuedCount += 1;
      } else if (run.status === "waiting") {
        summary.pendingCount += 1;
      }
      return summary;
    },
    { runningCount: 0, queuedCount: 0, pendingCount: 0 }
  );
}

export function codexLiveTaskHasVisibleActivity(state: CodexLiveTaskState) {
  const summary = codexLiveTaskSummary(state);
  return Boolean(
    summary.runningCount ||
      summary.queuedCount ||
      summary.pendingCount ||
      state.pendingTasks.length
  );
}

export function codexLiveTaskIsLoading(state: CodexLiveTaskState) {
  const summary = codexLiveTaskSummary(state);
  return summary.runningCount > 0 || summary.queuedCount > 0;
}

export type CodexLiveBridgeEvent = {
  type?: string;
  role?: "user" | "assistant";
  text?: string;
  message?: string;
  delegation_id?: string;
  workflow_run_id?: string;
  turn_id?: string;
  input_event_id?: string;
  duplicate_of?: string;
  delta?: string;
  activity?: AgentActivityEvent;
  running_count?: number;
  queued_count?: number;
  pending_count?: number;
  position?: number;
  reason?: string;
  route?: "ordinary_chat" | "learning_need" | "unclear";
  result?: RealtimeToolCallResponse;
};

function normalizedIdentifier(value: string | undefined) {
  return value?.trim() || null;
}

function workflowRunId(payload: CodexLiveBridgeEvent) {
  return normalizedIdentifier(payload.workflow_run_id) ?? normalizedIdentifier(payload.turn_id);
}

function runForEvent(state: CodexLiveTaskState, payload: CodexLiveBridgeEvent) {
  const runId = workflowRunId(payload);
  return runId ? state.runsByWorkflowId[runId] : undefined;
}

export function shouldPublishRealtimeToolTaskStatus(
  toolName: RealtimeToolName,
  result?: RealtimeToolCallResponse
) {
  if (toolName !== "run_chatbot_workflow") {
    return true;
  }
  return result?.model_output.route === "learning_need";
}

function eventRunStatus(payload: CodexLiveBridgeEvent): CodexLiveRunStatus | null {
  if (payload.type === "codex_live.workflow.input_pending") {
    return "waiting";
  }
  if (payload.type === "codex_live.workflow.queued") {
    return "queued";
  }
  if (
    payload.type === "codex_live.workflow.started" ||
    payload.type === "codex_live.workflow.progress" ||
    payload.type === "codex_live.workflow.output_delta"
  ) {
    return "running";
  }
  if (
    payload.type === "codex_live.workflow.cancelled" ||
    payload.type === "codex_live.workflow.dismissed" ||
    payload.type === "codex_live.workflow.duplicate"
  ) {
    return "cancelled";
  }
  if (payload.type === "codex_live.workflow.result" && payload.result) {
    return payload.result.status === "ok" && payload.result.model_output.status === "ok"
      ? "completed"
      : "error";
  }
  if (payload.type === "codex_live.workflow.error") {
    return "error";
  }
  return null;
}

function eventRunLabel(payload: CodexLiveBridgeEvent, status: CodexLiveRunStatus) {
  if (payload.activity?.label) {
    return payload.activity.label;
  }
  if (status === "waiting") {
    return "Waiting for confirmation of task processing method";
  }
  if (status === "queued") {
    return "Task queued";
  }
  if (status === "running") {
    return "Workflow is executing";
  }
  if (status === "completed") {
    return "Chatbot workflow completed";
  }
  if (status === "cancelled") {
    return "Task canceled";
  }
  return payload.message || "Chatbot workflow execution failed";
}

function shouldPreserveTerminalRun(
  previous: CodexLiveRunState | undefined,
  payload: CodexLiveBridgeEvent,
  nextStatus: CodexLiveRunStatus
) {
  if (!previous) {
    return false;
  }
  if (previous.documentResultSucceeded && nextStatus !== "completed") {
    return true;
  }
  if (!["completed", "cancelled", "error"].includes(previous.status)) {
    return false;
  }
  return payload.type !== "codex_live.workflow.result";
}

export function applyCodexLiveTaskEvent(
  state: CodexLiveTaskState,
  payload: CodexLiveBridgeEvent
): CodexLiveTaskState {
  let pendingTasks = state.pendingTasks;
  const delegationId = normalizedIdentifier(payload.delegation_id);
  if (payload.type === "codex_live.workflow.input_pending" && delegationId) {
    pendingTasks = [
      ...pendingTasks.filter((task) => task.delegationId !== delegationId),
      { delegationId, text: payload.text ?? "" },
    ];
  } else if (
    delegationId &&
    [
      "codex_live.workflow.queued",
      "codex_live.workflow.started",
      "codex_live.workflow.result",
      "codex_live.workflow.cancelled",
      "codex_live.workflow.dismissed",
      "codex_live.workflow.error",
    ].includes(payload.type ?? "")
  ) {
    pendingTasks = pendingTasks.filter((task) => task.delegationId !== delegationId);
  }

  const runId = workflowRunId(payload);
  if (payload.type === "codex_live.speech.error") {
    if (!runId) {
      return pendingTasks === state.pendingTasks ? state : { ...state, pendingTasks };
    }
    const previous = state.runsByWorkflowId[runId];
    if (!previous?.documentResultSucceeded) {
      return pendingTasks === state.pendingTasks ? state : { ...state, pendingTasks };
    }
    return {
      ...state,
      pendingTasks,
      runsByWorkflowId: {
        ...state.runsByWorkflowId,
        [runId]: {
          ...previous,
          speechStatus: "failed",
          label: "The workflow is completed, but the voice output is not completed",
        },
      },
    };
  }
  const status = eventRunStatus(payload);
  if (!runId || !status) {
    return pendingTasks === state.pendingTasks ? state : { ...state, pendingTasks };
  }

  const previous = state.runsByWorkflowId[runId];
  if (shouldPreserveTerminalRun(previous, payload, status)) {
    return {
      ...state,
      pendingTasks,
      runsByWorkflowId: {
        ...state.runsByWorkflowId,
        [runId]: previous,
      },
    };
  }

  const route = payload.route ?? previous?.route;
  const documentResultSucceeded =
    previous?.documentResultSucceeded === true || status === "completed";
  return {
    ...state,
    pendingTasks,
    runsByWorkflowId: {
      ...state.runsByWorkflowId,
      [runId]: {
        workflowRunId: runId,
        turnId: normalizedIdentifier(payload.turn_id) ?? previous?.turnId ?? runId,
        delegationId: delegationId ?? previous?.delegationId,
        status,
        route,
        label: eventRunLabel(payload, status),
        documentResultSucceeded,
        speechStatus: previous?.speechStatus ?? "not_requested",
        visibleAsTask: route !== "ordinary_chat" && route !== "unclear",
      },
    },
  };
}

export function resolveCodexLivePendingTask(
  state: CodexLiveTaskState,
  delegationId: string,
  action: CodexLiveTaskAction
): CodexLiveTaskState {
  const runsByWorkflowId: Record<string, CodexLiveRunState> = {};
  for (const [runId, run] of Object.entries(state.runsByWorkflowId)) {
    runsByWorkflowId[runId] =
      run.delegationId !== delegationId || run.status !== "waiting"
        ? run
        : {
            ...run,
            status: action === "dismiss" ? "cancelled" : "queued",
            label: action === "dismiss" ? "Task canceled" : "Updating task schedule",
          };
  }
  return {
    ...state,
    pendingTasks: state.pendingTasks.filter((task) => task.delegationId !== delegationId),
    runsByWorkflowId,
  };
}

type CodexLiveTaskEventContext = {
  lessonId: string;
  applyTaskEvent: (payload: CodexLiveBridgeEvent) => CodexLiveTaskState;
  delegationTurnId: (delegationId: string) => string;
  onToolStatusUpdate: (update: RealtimeToolStatusUpdate) => void;
  onToolResult: (lessonId: string, result: RealtimeToolCallResponse) => void;
  setVoiceStatusText: (value: string) => void;
  setError: (value: string) => void;
  logWorkflowStarted: (delegationId: string, turnId: string) => void;
};

export function handleCodexLiveTaskEvent(
  payload: CodexLiveBridgeEvent,
  context: CodexLiveTaskEventContext
) {
  const {
    lessonId,
    applyTaskEvent,
    delegationTurnId,
    onToolStatusUpdate,
    onToolResult,
    setVoiceStatusText,
    setError,
    logWorkflowStarted,
  } = context;
  if (payload.type === "codex_live.queue.updated") {
    return true;
  }

  const nextState = applyTaskEvent(payload);
  const delegationId = normalizedIdentifier(payload.delegation_id);
  const exactTurnId = normalizedIdentifier(payload.turn_id);
  const turnId = exactTurnId ?? (delegationId ? delegationTurnId(delegationId) : null);

  if (payload.type === "codex_live.workflow.input_pending" && delegationId && turnId) {
    const label = "New running utterance detected, please choose what to do";
    onToolStatusUpdate({ lessonId, turnId, delegationId, label, status: "waiting" });
    setVoiceStatusText(label);
    return true;
  }
  if (payload.type === "codex_live.workflow.duplicate" && delegationId && turnId) {
    onToolStatusUpdate({
      lessonId,
      turnId,
      delegationId,
      label: "Duplicate tasks have been ignored and the original tasks are still being executed.",
      status: "cancelled",
    });
    setVoiceStatusText("Duplicate instructions ignored");
    return true;
  }
  if (payload.type === "codex_live.workflow.queued" && delegationId && turnId) {
    const label = `Task queued${payload.position ? ` · position ${payload.position}` : ""}`;
    onToolStatusUpdate({ lessonId, turnId, delegationId, label, status: "queued" });
    setVoiceStatusText(label);
    return true;
  }
  if (payload.type === "codex_live.workflow.started" && delegationId && turnId) {
    const label = "Understanding tasks and preparing workflows";
    setVoiceStatusText(label);
    onToolStatusUpdate({ lessonId, turnId, delegationId, label, status: "running" });
    logWorkflowStarted(delegationId, turnId);
    return true;
  }
  if (payload.type === "codex_live.workflow.progress" && delegationId && turnId && payload.activity) {
    onToolStatusUpdate({
      lessonId,
      turnId,
      delegationId,
      label: payload.activity.label,
      status: payload.activity.status === "failed" ? "error" : "running",
      activity: payload.activity,
    });
    setVoiceStatusText(payload.activity.label);
    return true;
  }
  if (payload.type === "codex_live.workflow.output_delta" && delegationId && turnId) {
    onToolStatusUpdate({
      lessonId,
      turnId,
      delegationId,
      label: "Generating workflow results",
      status: "running",
    });
    return true;
  }
  if (payload.type === "codex_live.workflow.result" && payload.result && delegationId && turnId) {
    onToolResult(lessonId, payload.result);
    const succeeded = payload.result.status === "ok" && payload.result.model_output.status === "ok";
    const isBoardFreeRoute = payload.route === "ordinary_chat" || payload.route === "unclear";
    if (!isBoardFreeRoute) {
      const label = succeeded ? "Chatbot workflow completed" : "Chatbot workflow execution failed";
      onToolStatusUpdate({
        lessonId,
        turnId,
        delegationId,
        label,
        status: succeeded ? "completed" : "error",
      });
    }
    setVoiceStatusText(succeeded ? "Codex Live has completed this round of workflow" : "Chatbot workflow execution failed");
    return true;
  }
  if (
    ["codex_live.workflow.cancelled", "codex_live.workflow.dismissed"].includes(payload.type ?? "") &&
    delegationId &&
    turnId
  ) {
    if (payload.reason === "ordinary_chat") {
      setVoiceStatusText("Codex Live has completed normal conversation routing");
      return true;
    }
    onToolStatusUpdate({
      lessonId,
      turnId,
      delegationId,
      label: "Task canceled",
      status: "cancelled",
    });
    return true;
  }
  if (payload.type === "codex_live.speech.error") {
    const run = runForEvent(nextState, payload);
    if (run?.documentResultSucceeded) {
      const label = "The workflow is completed, but the voice output is not completed";
      if (run.visibleAsTask && turnId) {
        onToolStatusUpdate({
          lessonId,
          turnId,
          delegationId: delegationId ?? undefined,
          label,
          status: "completed",
        });
      }
      setVoiceStatusText(label);
    }
    return true;
  }
  if (payload.type === "codex_live.workflow.error" || payload.type === "codex_live.error") {
    const message = payload.message || "An error occurred in the Codex Live Chatbot workflow channel";
    const run = runForEvent(nextState, payload);
    if (run?.documentResultSucceeded) {
      return true;
    }
    onToolStatusUpdate({
      lessonId,
      turnId: turnId ?? `realtime-error-${crypto.randomUUID()}`,
      delegationId: delegationId ?? undefined,
      label: message,
      status: "error",
    });
    setError(message);
    return true;
  }
  return false;
}
