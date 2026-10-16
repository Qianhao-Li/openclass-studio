"use client";

import clsx from "clsx";
import { ChevronDown } from "lucide-react";
import { useState } from "react";

import {
  buildLearningRequirementDisplay,
  learningRequirementStatusLabel,
  type LearningRequirementDisplay,
  type LearningRequirementDisplayFactor,
} from "../lib/learning-requirement-display";
import type {
  CommitRecord,
  LearningClarificationStatus,
  LearningRequirementKeyFact,
  LearningRequirementSheet,
  Lesson,
} from "../types";

function compactText(value: string) {
  return value.replace(/\s+/g, " ").trim();
}

function clarityFromCommit(commit: CommitRecord): LearningClarificationStatus | null {
  const value = commit.metadata?.learning_clarification_after ?? commit.metadata?.learning_clarification;
  if (!value || typeof value !== "object") {
    return null;
  }
  const record = value as Partial<LearningClarificationStatus>;
  if (!Array.isArray(record.key_facts)) {
    return null;
  }

  return {
    progress: typeof record.progress === "number" ? record.progress : 0,
    label: typeof record.label === "string" ? record.label : "",
    reason: typeof record.reason === "string" ? record.reason : "",
    missing_items: Array.isArray(record.missing_items)
      ? record.missing_items.filter((item): item is string => typeof item === "string")
      : [],
    can_start: record.can_start === true,
    forced_start: record.forced_start === true,
    summary: typeof record.summary === "string" ? record.summary : "",
    key_facts: record.key_facts.flatMap((item) => {
      if (!item || typeof item !== "object") {
        return [];
      }
      const raw = item as unknown as Record<string, unknown>;
      if (typeof raw.label !== "string" || typeof raw.value !== "string") {
        return [];
      }
      return [
        {
          label: raw.label,
          value: raw.value,
          evidence: typeof raw.evidence === "string" ? raw.evidence : "",
          category: typeof raw.category === "string" ? (raw.category as LearningRequirementKeyFact["category"]) : null,
        },
      ];
    }),
    checklist: [],
    next_question: typeof record.next_question === "string" ? record.next_question : "",
    ready_for_board: record.ready_for_board === true,
    work_mode: record.work_mode ?? null,
    granularity: record.granularity ?? null,
  };
}

function lineageCommitIds(lesson: Lesson, targetCommitId: string | null | undefined) {
  const commitsById = new Map(lesson.history_graph.commits.map((commit) => [commit.id, commit]));
  const ids = new Set<string>();
  const stack = targetCommitId ? [targetCommitId] : [];

  while (stack.length) {
    const commitId = stack.pop();
    if (!commitId || ids.has(commitId)) {
      continue;
    }
    ids.add(commitId);
    commitsById.get(commitId)?.parent_ids.forEach((parentId) => stack.push(parentId));
  }

  return ids;
}

function collectFacts(
  clarityStatus: LearningClarificationStatus,
  lesson: Lesson | null | undefined,
  targetCommitId: string | null | undefined
) {
  const facts: LearningRequirementKeyFact[] = [];
  if (lesson) {
    const lineageIds = lineageCommitIds(lesson, targetCommitId);
    lesson.history_graph.commits.forEach((commit) => {
      if (!lineageIds.has(commit.id)) {
        return;
      }
      const clarity = clarityFromCommit(commit);
      if (clarity) {
        facts.push(...clarity.key_facts);
      }
    });
  }
  facts.push(...clarityStatus.key_facts);
  return facts;
}

function FactorRows({ factors }: { factors: LearningRequirementDisplayFactor[] }) {
  return (
    <dl className="mt-2 space-y-2 text-xs leading-6">
      {factors.map((factor) => (
        <div key={factor.key} className="grid grid-cols-[74px_minmax(0,1fr)] gap-3">
          <dt className={clsx("font-semibold", factor.required ? "text-blue-700" : "text-blue-600/80")}>
            {factor.label}
          </dt>
          <dd className={clsx("min-w-0 break-words", factor.filled ? "text-blue-950" : "text-blue-700/70")}>
            {factor.value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function DisplaySections({ display }: { display: LearningRequirementDisplay }) {
  const visibleAuxiliaryFactors = display.auxiliaryFactors.slice(0, 2);
  const hiddenAuxiliaryCount = Math.max(0, display.auxiliaryFactors.length - visibleAuxiliaryFactors.length);

  return (
    <div className="mt-4 space-y-4">
      <section>
        <div className="flex items-center justify-between gap-3">
          <p className="text-[11px] font-bold uppercase tracking-widest text-blue-700">core factors</p>
          <span className="rounded-full bg-white px-2 py-0.5 text-[10px] font-semibold text-blue-700">
            {display.coreFactors.every((factor) => factor.filled) ? "complete" : "To be improved"}
          </span>
        </div>
        <FactorRows factors={display.coreFactors} />
      </section>

      {visibleAuxiliaryFactors.length ? (
        <section>
          <div className="flex items-center justify-between gap-3">
            <p className="text-[11px] font-bold uppercase tracking-widest text-blue-500">auxiliary factors</p>
            {hiddenAuxiliaryCount ? (
              <span className="text-[10px] font-medium text-blue-500">Also {hiddenAuxiliaryCount}  Details on the right</span>
            ) : null}
          </div>
          <FactorRows factors={visibleAuxiliaryFactors} />
        </section>
      ) : null}
    </div>
  );
}

interface LearningClarityCardProps {
  barTone: string;
  clarityStatus: LearningClarificationStatus;
  activeRequirementSheet?: LearningRequirementSheet | null;
  lesson?: Lesson | null;
  targetCommitId?: string | null;
  statusLabelOverride?: string;
  statusMessageOverride?: string;
  statusTone?: "default" | "success" | "error";
}

export function LearningClarityCard({
  barTone,
  clarityStatus,
  activeRequirementSheet,
  lesson,
  targetCommitId,
  statusLabelOverride,
  statusMessageOverride,
  statusTone = "default",
}: LearningClarityCardProps) {
  const [expanded, setExpanded] = useState(false);
  const display = buildLearningRequirementDisplay({
    requirementSheet: activeRequirementSheet,
    clarification: clarityStatus,
    keyFacts: collectFacts(clarityStatus, lesson, targetCommitId),
  });
  const statusLabel = statusLabelOverride ?? learningRequirementStatusLabel(display.status);

  return (
    <div className="rounded-xl border border-blue-100/50 bg-[#f4f6ff] p-4">
      <button
        type="button"
        aria-expanded={expanded}
        aria-label={expanded ? "Collapse teaching checklist" : "Expand teaching checklist"}
        onClick={() => setExpanded((current) => !current)}
        className="flex w-full items-center gap-3 rounded-lg text-left outline-none transition focus-visible:ring-2 focus-visible:ring-blue-400 focus-visible:ring-offset-2 focus-visible:ring-offset-[#f4f6ff]"
      >
        <div
          role="progressbar"
          aria-label="Teaching checklist progress"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={display.progress}
          className="h-2 flex-1 overflow-hidden rounded-full bg-white shadow-inner"
        >
          <div
            className={clsx("h-full rounded-full transition-all duration-500", barTone)}
            style={{ width: `${display.progress}%` }}
          />
        </div>
        <span className="inline-flex items-center gap-1 rounded-full bg-white px-2.5 py-1 text-[11px] font-bold text-blue-800 shadow-sm">
          {display.progress}%
          <ChevronDown
            aria-hidden="true"
            className={clsx("h-3 w-3 transition-transform", expanded && "rotate-180")}
          />
        </span>
      </button>

      {expanded ? (
        <>
          <div className="mt-4 flex items-start justify-between gap-3">
            <div>
              <p className="text-[10px] font-bold uppercase tracking-widest text-blue-500">Teaching type</p>
              <p className="mt-1 text-sm font-semibold text-blue-950">{display.teachingType}</p>
            </div>
            <span
              className={clsx(
                "rounded-full px-2.5 py-1 text-[11px] font-semibold",
                statusTone === "error"
                  ? "bg-red-50 text-red-700"
                  : statusTone === "success" || statusLabelOverride || display.status === "ready"
                    ? "bg-emerald-50 text-emerald-700"
                    : "bg-white text-blue-700"
              )}
            >
              {statusLabel}
            </span>
          </div>

          {statusMessageOverride ? (
            <p className="mt-3 rounded-lg border border-red-100 bg-red-50 px-3 py-2 text-xs leading-5 text-red-800">
              {statusMessageOverride}
            </p>
          ) : null}
          {display.summary ? <p className="mt-3 text-xs leading-6 text-blue-900/80">{compactText(display.summary)}</p> : null}
          <DisplaySections display={display} />
        </>
      ) : null}
    </div>
  );
}
