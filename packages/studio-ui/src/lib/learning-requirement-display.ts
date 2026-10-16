import type {
  BoardWorkflow,
  InitialLearningGranularity,
  InitialLearningWorkMode,
  LearningClarificationStatus,
  LearningRequirementKeyFact,
  LearningRequirementSheet,
} from "../types";
import { sourceReferenceRangeDisplayLabel } from "./source-range-display";

export type LearningRequirementDisplayStatus = "collecting" | "ready" | "unknown";
export type LearningRequirementTeachingType = "Teaching new knowledge points" | "practise" | "Undetermined";

export interface LearningRequirementDisplayFactor {
  key: string;
  label: string;
  value: string;
  filled: boolean;
  required: boolean;
}

export interface LearningRequirementDisplay {
  teachingType: LearningRequirementTeachingType;
  status: LearningRequirementDisplayStatus;
  progress: number;
  summary: string;
  coreFactors: LearningRequirementDisplayFactor[];
  auxiliaryFactors: LearningRequirementDisplayFactor[];
}

type BuildLearningRequirementDisplayInput = {
  requirementSheet?: LearningRequirementSheet | null;
  clarification?: LearningClarificationStatus | null;
  keyFacts?: LearningRequirementKeyFact[];
};

const STATUS_LABELS: Record<LearningRequirementDisplayStatus, string> = {
  collecting: "Collecting",
  ready: "Complete",
  unknown: "Undetermined",
};

export function learningRequirementStatusLabel(status: LearningRequirementDisplayStatus) {
  return STATUS_LABELS[status];
}

export function buildLearningRequirementDisplay({
  requirementSheet,
  clarification,
  keyFacts = [],
}: BuildLearningRequirementDisplayInput): LearningRequirementDisplay {
  const facts = dedupeFacts([...(keyFacts ?? []), ...(clarification?.key_facts ?? [])]);
  const workMode = normalizeWorkMode(requirementSheet?.work_mode ?? clarification?.work_mode ?? null);
  const granularity = normalizeGranularity(requirementSheet?.granularity ?? clarification?.granularity ?? null);
  const learningGoal = meaningfulText(requirementSheet?.learning_goal ?? latestFactByCategory(facts, "learning")?.value);
  const progress = Math.max(0, Math.min(100, clarification?.progress ?? 0));
  const boardWorkflowFactor = buildBoardWorkflowFactor(requirementSheet?.board_workflow ?? null);

  if (workMode === "knowledge_board") {
    const hasStructuredSourceScope = granularity === "source_chapter";
    const isSingleKnowledgePoint = granularity === "single_knowledge_point" || hasStructuredSourceScope;
    const knowledgePoint = isSingleKnowledgePoint && learningGoal ? learningGoal : "To be converged to specific knowledge points";
    const coreFactors = [
      boardWorkflowFactor,
      factor({
        key: "knowledge_point",
        label: hasStructuredSourceScope ? "Information section" : "Knowledge points",
        value: knowledgePoint,
        filled: Boolean(isSingleKnowledgePoint && learningGoal),
      }),
    ];
    return {
      teachingType: "Teaching new knowledge points",
      status: displayStatus(coreFactors, clarification),
      progress: displayProgress(progress, coreFactors, clarification),
      summary: requirementSummary(
        requirementSheet,
        clarification?.summary || clarification?.reason || learningGoal || ""
      ),
      coreFactors,
      auxiliaryFactors: buildAuxiliaryFactors({
        requirementSheet,
        facts,
        usedValues: [knowledgePoint],
        broadTopic: !isSingleKnowledgePoint ? learningGoal : "",
      }),
    };
  }

  if (workMode === "practice_artifact") {
    const currentLevel = meaningfulText(latestFactByCategory(facts, "level")?.value) || meaningfulText(requirementSheet?.level);
    const targetScenario = meaningfulText(latestFactByCategory(facts, "scenario")?.value, { allowNoScenario: true });
    const coreFactors = [
      boardWorkflowFactor,
      factor({
        key: "practice_content",
        label: "Exercise content",
        value: learningGoal || "To be clarified",
        filled: Boolean(learningGoal),
      }),
      factor({
        key: "current_level",
        label: "current level",
        value: currentLevel || "To be clarified",
        filled: Boolean(currentLevel),
      }),
      factor({
        key: "target_scenario",
        label: "target scene",
        value: targetScenario || "To be clarified",
        filled: Boolean(targetScenario),
      }),
    ];
    return {
      teachingType: "practise",
      status: displayStatus(coreFactors, clarification),
      progress: displayProgress(progress, coreFactors, clarification),
      summary: requirementSummary(
        requirementSheet,
        clarification?.summary || clarification?.reason || learningGoal || ""
      ),
      coreFactors,
      auxiliaryFactors: buildAuxiliaryFactors({
        requirementSheet,
        facts,
        usedValues: [learningGoal, currentLevel, targetScenario],
      }),
    };
  }

  const coreFactors = [
    boardWorkflowFactor,
    factor({
      key: "learning_type",
      label: "learning type",
      value: "To be judged",
      filled: false,
    }),
  ];
  return {
    teachingType: "Undetermined",
    status: "unknown",
    progress,
    summary: requirementSummary(
      requirementSheet,
      clarification?.summary || clarification?.reason || ""
    ),
    coreFactors,
    auxiliaryFactors: buildAuxiliaryFactors({
      requirementSheet,
      facts,
      usedValues: [],
    }),
  };
}

export function boardWorkflowLabel(value: BoardWorkflow | null | undefined) {
  const workflow = normalizeBoardWorkflow(value);
  if (workflow === "generate_from_scratch") {
    return "Generate a new board";
  }
  if (workflow === "act_on_existing_board") {
    return "Perform actions on existing board content";
  }
  return "To be judged";
}

function buildBoardWorkflowFactor(value: BoardWorkflow | null | undefined) {
  const workflow = normalizeBoardWorkflow(value);
  return factor({
    key: "board_workflow",
    label: "board link",
    value: boardWorkflowLabel(workflow),
    filled: workflow !== "unknown",
  });
}

function normalizeBoardWorkflow(value: BoardWorkflow | null | undefined): BoardWorkflow {
  if (value === "generate_from_scratch" || value === "act_on_existing_board") {
    return value;
  }
  return "unknown";
}

function normalizeWorkMode(value: InitialLearningWorkMode | null | undefined): InitialLearningWorkMode | "unknown" {
  if (value === "knowledge_board" || value === "narrow_topic") {
    return "knowledge_board";
  }
  if (value === "practice_artifact") {
    return "practice_artifact";
  }
  return "unknown";
}

function normalizeGranularity(value: InitialLearningGranularity | null | undefined): InitialLearningGranularity | "unclear" {
  if (value === "single_knowledge_point" || value === "source_chapter" || value === "broad_topic" || value === "practice_artifact") {
    return value;
  }
  return "unclear";
}

function factor({
  key,
  label,
  value,
  filled,
  required = true,
}: {
  key: string;
  label: string;
  value: string;
  filled: boolean;
  required?: boolean;
}): LearningRequirementDisplayFactor {
  return {
    key,
    label,
    value: compactText(value),
    filled,
    required,
  };
}

function displayStatus(
  coreFactors: LearningRequirementDisplayFactor[],
  clarification?: LearningClarificationStatus | null
): LearningRequirementDisplayStatus {
  if (clarification?.ready_for_board) {
    return "ready";
  }
  return coreFactors.every((item) => item.filled) ? "ready" : "collecting";
}

function displayProgress(
  progress: number,
  coreFactors: LearningRequirementDisplayFactor[],
  clarification?: LearningClarificationStatus | null
) {
  if (clarification?.ready_for_board || coreFactors.every((item) => item.filled)) {
    return Math.max(progress, 100);
  }
  return progress;
}

function buildAuxiliaryFactors({
  requirementSheet,
  facts,
  usedValues,
  broadTopic,
}: {
  requirementSheet?: LearningRequirementSheet | null;
  facts: LearningRequirementKeyFact[];
  usedValues: Array<string | undefined>;
  broadTopic?: string;
}) {
  const used = new Set(usedValues.map((value) => normalizeText(value ?? "")).filter(Boolean));
  const factors: LearningRequirementDisplayFactor[] = [];

  pushAuxiliary(factors, used, "confirmed_source", "Confirmed information", confirmedSourceLabel(requirementSheet));
  pushAuxiliary(factors, used, "broad_topic", "current direction", broadTopic);
  pushAuxiliary(factors, used, "known_background", "Already have background", requirementSheet?.known_background);
  pushAuxiliary(factors, used, "target_depth", "target depth", requirementSheet?.target_depth);
  pushAuxiliary(factors, used, "output_preference", "Output preferences", requirementSheet?.output_preference);

  for (const factItem of facts) {
    const label = auxiliaryLabelForFact(factItem);
    if (!label) {
      continue;
    }
    pushAuxiliary(factors, used, `fact_${label}`, label, factItem.value);
  }

  return factors.slice(0, 6);
}

function confirmedSourceLabel(requirementSheet?: LearningRequirementSheet | null) {
  const grounding = requirementSheet?.source_grounding;
  if (grounding?.confirmation_status !== "confirmed" || !grounding.confirmed_references.length) {
    return "";
  }
  const reference = grounding.confirmed_references[0];
  const location = [
    reference.source_title,
    reference.section_path.join(" > "),
    sourceReferenceRangeDisplayLabel({
      pageRange: reference.page_range,
      sourceLocator: reference.source_locator,
    }),
  ]
    .filter(Boolean)
    .join(" / ");
  const remaining = grounding.confirmed_references.length - 1;
  return remaining > 0 ? `${location} and ${grounding.confirmed_references.length - 1} more` : location;
}

function requirementSummary(
  requirementSheet: LearningRequirementSheet | null | undefined,
  value: string
) {
  let summary = compactText(value);
  const references = requirementSheet?.source_grounding?.confirmed_references ?? [];
  for (const reference of references) {
    const storedRange = reference.page_range.trim();
    if (!storedRange || !summary.includes(storedRange)) {
      continue;
    }
    const displayRange = sourceReferenceRangeDisplayLabel({
      pageRange: storedRange,
      sourceLocator: reference.source_locator,
    });
    summary = summary.replaceAll(storedRange, displayRange);
  }
  return summary;
}

function pushAuxiliary(
  factors: LearningRequirementDisplayFactor[],
  used: Set<string>,
  key: string,
  label: string,
  rawValue?: string | null
) {
  const value = meaningfulText(rawValue, { allowNoScenario: true });
  const normalized = normalizeText(value);
  if (!value || used.has(normalized)) {
    return;
  }
  used.add(normalized);
  factors.push(
    factor({
      key,
      label,
      value,
      filled: true,
      required: false,
    })
  );
}

function auxiliaryLabelForFact(factItem: LearningRequirementKeyFact) {
  if (factItem.category === "vocabulary") {
    return "Vocabulary";
  }
  if (factItem.category === "output") {
    return "Output requirements";
  }
  if (factItem.category === "other") {
    return compactText(factItem.label);
  }
  return "";
}

function latestFactByCategory(facts: LearningRequirementKeyFact[], category: NonNullable<LearningRequirementKeyFact["category"]>) {
  for (let index = facts.length - 1; index >= 0; index -= 1) {
    const factItem = facts[index];
    if (factItem.category === category || legacyCategoryFromLabel(factItem) === category) {
      return factItem;
    }
  }
  return null;
}

function legacyCategoryFromLabel(factItem: LearningRequirementKeyFact): LearningRequirementKeyFact["category"] {
  const label = normalizeText(factItem.label);
  if (["Learning content", "learning topics", "learning objectives", "learning direction", "Want to learn", "Specific content", "Knowledge points"].some((item) => label.includes(item))) {
    return "learning";
  }
  if (["current level", "own level", "Already have a foundation", "Base", "level"].some((item) => label.includes(item))) {
    return "level";
  }
  if (["Scenario oriented", "Usage scenarios", "Application scenarios", "target scene", "scene"].some((item) => label.includes(item))) {
    return "scenario";
  }
  if (["Vocabulary", "vocabulary"].some((item) => label.includes(item))) {
    return "vocabulary";
  }
  if (["Output requirements", "output demand", "target output", "output"].some((item) => label.includes(item))) {
    return "output";
  }
  return "other";
}

function dedupeFacts(facts: LearningRequirementKeyFact[]) {
  const seen = new Set<string>();
  return facts.filter((factItem) => {
    const label = compactText(factItem.label);
    const value = compactText(factItem.value);
    if (!label || !value) {
      return false;
    }
    const key = `${normalizeText(label)}:${normalizeText(value)}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}

function meaningfulText(value: string | null | undefined, options: { allowNoScenario?: boolean } = {}) {
  const text = compactText(value ?? "");
  if (!text) {
    return "";
  }
  if (options.allowNoScenario && text === "无明确应用场景") {
    return text;
  }
  if (
    text.includes("待确认") ||
    text.includes("尚未明确") ||
    text.includes("尚未完全明确") ||
    text.includes("动态决定") ||
    text.includes("根据用户")
  ) {
    return "";
  }
  return text;
}

function compactText(value: string) {
  return value.replace(/\s+/g, " ").trim();
}

function normalizeText(value: string) {
  return compactText(value).replace(/[：:，,。.；;\s/_-]+/g, "").toLowerCase();
}
