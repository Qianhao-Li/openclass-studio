import {
  getLessonCommit,
  learningClarityFromCommit,
} from "./history-utils";
import type {
  BoardTaskRequirementSheet,
  CommitRecord,
  LearningClarificationStatus,
  LearningRequirementSheet,
  Lesson,
} from "../../types";

export type ExecutedNeedSnapshot =
  | {
      kind: "board_task";
      boardTask: BoardTaskRequirementSheet;
      commit: CommitRecord;
    }
  | {
      kind: "learning_requirement";
      requirementSheet: LearningRequirementSheet;
      clarityStatus: LearningClarificationStatus;
      commit: CommitRecord;
    };

export type GenerationFailureSnapshot = {
  commit: CommitRecord;
  reason: string;
};

const DEFAULT_GENERATION_FAILURE_REASON =
  "Board generation did not finish. The confirmed learning requirements were retained and can be retried.";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function boardTaskFromMetadata(value: unknown): BoardTaskRequirementSheet | null {
  if (!isRecord(value) || typeof value.target_hint !== "string" || typeof value.question_or_topic !== "string") {
    return null;
  }
  return value as unknown as BoardTaskRequirementSheet;
}

function requirementSheetFromMetadata(value: unknown): LearningRequirementSheet | null {
  if (!isRecord(value) || typeof value.theme !== "string" || typeof value.learning_goal !== "string") {
    return null;
  }
  return value as unknown as LearningRequirementSheet;
}

function lineageCommitsNewestFirst(lesson: Lesson, targetCommitId: string | null) {
  const commitsById = new Map(lesson.history_graph.commits.map((commit) => [commit.id, commit]));
  const visited = new Set<string>();
  const ordered: CommitRecord[] = [];
  const stack = targetCommitId ? [targetCommitId] : [];

  while (stack.length) {
    const commitId = stack.pop();
    if (!commitId || visited.has(commitId)) {
      continue;
    }
    visited.add(commitId);
    const commit = commitsById.get(commitId);
    if (!commit) {
      continue;
    }
    ordered.push(commit);
    commit.parent_ids.forEach((parentId) => stack.push(parentId));
  }

  return ordered;
}

function latestRequirementSheetBefore(commits: CommitRecord[], startIndex: number) {
  for (let index = startIndex; index < commits.length; index += 1) {
    const metadata = commits[index].metadata ?? {};
    const sheet =
      requirementSheetFromMetadata(metadata.active_requirement_sheet_after) ??
      requirementSheetFromMetadata(metadata.learning_requirement_sheet);
    if (sheet) {
      return sheet;
    }
  }
  return null;
}

function latestClarityBefore(commits: CommitRecord[], startIndex: number) {
  for (let index = startIndex; index < commits.length; index += 1) {
    const clarity = learningClarityFromCommit(commits[index]);
    if (clarity) {
      return clarity;
    }
  }
  return null;
}

function executedRequirementClarity(
  sheet: LearningRequirementSheet,
  clarity: LearningClarificationStatus | null
): LearningClarificationStatus {
  return {
    progress: 100,
    label: "has been executed",
    reason: clarity?.reason || "This list of learning needs has been used to generate board content.",
    missing_items: [],
    can_start: true,
    forced_start: clarity?.forced_start === true,
    summary: clarity?.summary || sheet.learning_goal || sheet.theme,
    key_facts: clarity?.key_facts ?? [],
    checklist: clarity?.checklist ?? [],
    next_question: "",
    ready_for_board: true,
    work_mode: sheet.work_mode ?? clarity?.work_mode ?? null,
    granularity: sheet.granularity ?? clarity?.granularity ?? null,
  };
}

export function latestExecutedNeedSnapshot(
  lesson: Lesson,
  targetCommitId: string | null
): ExecutedNeedSnapshot | null {
  const targetCommit = getLessonCommit(lesson, targetCommitId) ?? lesson.history_graph.commits.at(-1) ?? null;
  const commits = lineageCommitsNewestFirst(lesson, targetCommit?.id ?? null);

  for (let index = 0; index < commits.length; index += 1) {
    const commit = commits[index];
    const metadata = commit.metadata ?? {};
    const boardTask = boardTaskFromMetadata(metadata.board_task_sheet);
    if (metadata.board_task_cleared === true && boardTask) {
      return { kind: "board_task", boardTask, commit };
    }

    const isExecutedRequirement =
      metadata.requirement_cleared === true &&
      (metadata.kind === "board_document_generation" || metadata.board_generation_action === "start");
    if (!isExecutedRequirement) {
      continue;
    }

    const requirementSheet =
      requirementSheetFromMetadata(metadata.learning_requirement_sheet) ??
      latestRequirementSheetBefore(commits, index + 1);
    if (!requirementSheet) {
      continue;
    }
    const clarity = learningClarityFromCommit(commit) ?? latestClarityBefore(commits, index + 1);
    return {
      kind: "learning_requirement",
      requirementSheet,
      clarityStatus: executedRequirementClarity(requirementSheet, clarity),
      commit,
    };
  }

  return null;
}

export function latestGenerationFailureSnapshot(
  lesson: Lesson,
  targetCommitId: string | null
): GenerationFailureSnapshot | null {
  const targetCommit = getLessonCommit(lesson, targetCommitId) ?? lesson.history_graph.commits.at(-1) ?? null;
  const commits = lineageCommitsNewestFirst(lesson, targetCommit?.id ?? null);

  for (const commit of commits) {
    const metadata = commit.metadata ?? {};
    if (metadata.kind === "board_document_generation" && metadata.requirement_cleared === true) {
      return null;
    }
    if (metadata.kind === "learning_requirement_generation_failed") {
      const reason = metadata.generation_failure_reason;
      return {
        commit,
        reason:
          typeof reason === "string" && reason.trim()
            ? reason.trim()
            : DEFAULT_GENERATION_FAILURE_REASON,
      };
    }
    if (
      metadata.kind === "learning_requirement_completed" ||
      metadata.kind === "learning_requirement_forced_ready" ||
      metadata.kind === "learning_requirement_frozen" ||
      metadata.kind === "learning_requirement_forced_frozen" ||
      (metadata.kind === "learning_requirement_refinement" && metadata.requirement_changed === true)
    ) {
      return null;
    }
  }

  return null;
}
