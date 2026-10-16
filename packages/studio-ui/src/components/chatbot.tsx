"use client";

import { sourceCitationDisplayNumber } from "./course-studio/chat-source-references";
import { ChatSelectionPreviews, selectionPreviewText } from "./course-studio/chat-selection-previews";

import { useState } from "react";
import Image from "next/image";
import clsx from "clsx";
import {
  ArrowRight,
  Check,
  ChevronDown,
  ChevronUp,
  Copy,
  GitFork,
  LoaderCircle,
  MessageSquare,
  MoveUpRight,
  PencilLine,
  RefreshCcw,
  Trash2,
  X,
} from "lucide-react";

import {
  normalizeAgentActivityEvents,
  publicAgentActivityLabel,
  publicAgentActivityText,
} from "../lib/agent-activity";
import { writeTextToClipboard } from "../lib/clipboard";
import { markdownToChatHtml } from "../lib/markdown";
import type {
  AgentActivityEvent,
  ChatInteractionMode,
  GuidedRequirementDiscovery,
  SectionTeachingProgress,
  SelectionRef,
  SourceCitation,
} from "../types";

export type CourseChatMessageView = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status?: "ready" | "pending" | "error";
  statusLabel?: string;
  selection?: SelectionRef | null;
  sourceSelections?: SelectionRef[];
  agentActivity?: AgentActivityEvent[];
  guidedRequirementDiscovery?: GuidedRequirementDiscovery | null;
  followUpSuggestions?: string[];
  teachingProgress?: SectionTeachingProgress | null;
  commitId?: string | null;
  parentCommitIds?: string[];
  editableContent?: string;
  interactionMode?: ChatInteractionMode;
  editedFromCommitId?: string | null;
  sourceCitations?: SourceCitation[];
};

export function chatMessageSelections(message: CourseChatMessageView): SelectionRef[] {
  return [
    ...(message.sourceSelections ?? []),
    ...(message.selection ? [message.selection] : []),
  ];
}

function ChatMessageContent({ content }: { content: string }) {
  return (
    <div
      className="chat-markdown space-y-3 [&>*+*]:mt-3 [&_code:not(.hljs)]:rounded [&_code:not(.hljs)]:bg-black/5 [&_code:not(.hljs)]:px-1 [&_code:not(.hljs)]:py-0.5 [&_code:not(.hljs)]:font-mono [&_code:not(.hljs)]:text-[0.92em] [&_em]:italic [&_strong]:font-semibold"
      dangerouslySetInnerHTML={{ __html: markdownToChatHtml(content) }}
    />
  );
}

function activityDetail(event: AgentActivityEvent): string {
  const detail = event.metadata.detail;
  if (typeof detail === "string" && detail.trim()) {
    return publicAgentActivityText(detail.trim());
  }
  const command = event.metadata.command;
  if (typeof command === "string" && command.trim()) {
    return command.trim();
  }
  const query = event.metadata.query;
  if (typeof query === "string" && query.trim()) {
    return query.trim();
  }
  const changes = event.metadata.changes;
  if (Array.isArray(changes) && changes.length) {
    return JSON.stringify(changes, null, 2);
  }
  const result = event.metadata.result;
  if (result !== undefined && result !== null) {
    return typeof result === "string" ? result : JSON.stringify(result, null, 2);
  }
  return "";
}

function AgentActivityTimeline({ events, isPending }: { events: AgentActivityEvent[]; isPending: boolean }) {
  const [expandedOverride, setExpandedOverride] = useState<boolean | null>(null);
  const isExpanded = expandedOverride ?? isPending;
  const normalizedEvents = normalizeAgentActivityEvents(events);
  if (!normalizedEvents.length) {
    return null;
  }
  const latestEvent = normalizedEvents[normalizedEvents.length - 1];
  const hasActiveEvent = normalizedEvents.some((event) => event.status === "pending" || event.status === "running");
  const problemCount = normalizedEvents.filter((event) => event.status === "blocked" || event.status === "failed").length;
  const summary = isPending || hasActiveEvent
    ? publicAgentActivityLabel(latestEvent.label)
    : problemCount
      ? `${normalizedEvents.length} tasks, ${problemCount} incomplete`
      : `${normalizedEvents.length} tasks completed`;

  return (
    <div className="overflow-hidden rounded-xl border border-gray-200 bg-gray-50/80">
      <button
        type="button"
        aria-expanded={isExpanded}
        onClick={() => setExpandedOverride(!isExpanded)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left transition hover:bg-gray-100"
      >
        <span className="flex h-5 w-5 shrink-0 items-center justify-center text-gray-500">
          {isPending || hasActiveEvent ? (
            <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
          ) : isExpanded ? (
            <ChevronUp className="h-3.5 w-3.5" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5" />
          )}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[11px] font-semibold text-gray-800">working process</span>
          <span className="block truncate text-[11px] leading-5 text-gray-500">{summary}</span>
        </span>
        <span className="text-[10px] font-medium text-gray-400">{isExpanded ? "close" : "Expand"}</span>
      </button>
      {isExpanded ? <div className="space-y-2 border-t border-gray-200 px-3 py-2.5">
        {normalizedEvents.map((event) => {
          const isActive = event.status === "pending" || event.status === "running";
          const isProblem = event.status === "blocked" || event.status === "failed";
          const detail = activityDetail(event);
          const command = typeof event.metadata.command === "string" ? event.metadata.command.trim() : "";
          const cwd = typeof event.metadata.cwd === "string" ? event.metadata.cwd.trim() : "";
          return (
            <div key={event.id} className="flex min-w-0 items-start gap-2 text-[11px] leading-5 text-gray-600">
              <span
                className={clsx(
                  "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border bg-white",
                  isProblem ? "border-amber-300 text-amber-700" : "border-gray-200 text-gray-500"
                )}
              >
                {isActive ? (
                  <LoaderCircle className="h-3 w-3 animate-spin" />
                ) : isProblem ? (
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500" />
                ) : (
                  <Check className="h-3 w-3" />
                )}
              </span>
              <div className="min-w-0 flex-1">
                <p className="font-medium text-gray-700">{publicAgentActivityLabel(event.label)}</p>
                {command && detail !== command ? (
                  <pre className="custom-scrollbar mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md bg-white px-2 py-1.5 font-mono text-[10px] leading-4 text-gray-700">
                    {command}
                  </pre>
                ) : null}
                {cwd ? <p className="mt-1 truncate font-mono text-[10px] text-gray-400">{cwd}</p> : null}
                {detail ? (
                  <pre className="custom-scrollbar mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded-md bg-white px-2 py-1.5 font-sans text-[11px] leading-5 text-gray-600">
                    {detail}
                  </pre>
                ) : null}
              </div>
            </div>
          );
        })}
      </div> : null}
    </div>
  );
}

export function CourseChatMessage({
  message,
  onContinueTeaching,
  onFollowUpSuggestion,
  onStartEdit,
  onRegenerate,
  onFork,
  onDelete,
  deleteLabel = "Hide conversation",
  deleteDisabledReason,
  isEditing = false,
  editingContent = "",
  onEditingContentChange,
  onCancelEdit,
  onSubmitEdit,
  onOpenSourceCitation,
  isEditDisabled = false,
}: {
  message: CourseChatMessageView;
  onContinueTeaching?: () => void;
  onFollowUpSuggestion?: (suggestion: string) => void;
  onStartEdit?: () => void;
  onRegenerate?: () => void;
  onFork?: () => void;
  onDelete?: () => void;
  deleteLabel?: string;
  deleteDisabledReason?: string | null;
  isEditing?: boolean;
  editingContent?: string;
  onEditingContentChange?: (value: string) => void;
  onCancelEdit?: () => void;
  onSubmitEdit?: () => void;
  onOpenSourceCitation?: (citation: SourceCitation) => void | Promise<void>;
  isEditDisabled?: boolean;
}) {
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "error">("idle");
  const isAssistant = message.role === "assistant";
  const isPending = message.status === "pending";
  const isError = message.status === "error";
  const displayedSelections = chatMessageSelections(message);
  const teachingProgress = message.teachingProgress;
  const agentActivity = message.agentActivity ?? [];
  const followUpSuggestions = message.followUpSuggestions ?? [];
  const sourceCitations = message.sourceCitations ?? [];
  const hasContent = message.content.trim().length > 0;

  async function copyMessage() {
    if (!hasContent) {
      return;
    }
    const succeeded = await writeTextToClipboard(message.content);
    setCopyStatus(succeeded ? "copied" : "error");
    window.setTimeout(() => setCopyStatus("idle"), 1600);
  }

  return (
    <article className={clsx("group flex gap-3", !isAssistant && "justify-end")}>
      {isAssistant ? (
        <div className="mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-gray-200 bg-white text-gray-900 shadow-sm">
          {isPending ? (
            <LoaderCircle className="h-3.5 w-3.5 animate-spin" />
          ) : (
            <Image src="/icon.png" alt="" width={20} height={20} className="h-5 w-5 rounded-md" />
          )}
        </div>
      ) : null}

      <div className={clsx("min-w-0 max-w-[86%] space-y-2", isAssistant && "max-w-[calc(100%-2.5rem)] flex-1")}>
        <div className={clsx("flex items-center gap-2 text-[11px] text-gray-500", !isAssistant && "justify-end")}>
          {!isAssistant ? <MessageSquare className="h-3.5 w-3.5" /> : null}
          <span className="font-medium">{isPending ? message.statusLabel || "thinking" : isAssistant ? "OpenClass" : "you"}</span>
        </div>

        {displayedSelections.length ? (
          <div className={clsx("space-y-2", !isAssistant && "ml-auto")}>
            <ChatSelectionPreviews selections={displayedSelections} />
          </div>
        ) : null}

        {isAssistant && agentActivity.length ? (
          <AgentActivityTimeline events={agentActivity} isPending={isPending} />
        ) : null}

        {isEditing && !isAssistant ? (
          <div
            className="ml-auto rounded-2xl rounded-tr-md border border-gray-200 bg-white p-2 shadow-sm"
            onMouseUp={(event) => event.stopPropagation()}
          >
            <textarea
              value={editingContent}
              autoFocus
              rows={3}
              onChange={(event) => onEditingContentChange?.(event.target.value)}
              onKeyDown={(event) => {
                if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                  event.preventDefault();
                  onSubmitEdit?.();
                }
              }}
              className="custom-scrollbar block max-h-44 min-h-24 w-[min(28rem,calc(100vw-5rem))] resize-y rounded-xl border border-gray-200 bg-gray-50 px-3 py-2 text-[13px] leading-6 text-gray-900 outline-none transition focus:border-gray-900 focus:bg-white"
            />
            <div className="mt-2 flex justify-end gap-1.5">
              <button
                type="button"
                onClick={onCancelEdit}
                className="flex h-8 w-8 items-center justify-center rounded-md text-gray-400 transition hover:bg-gray-100 hover:text-gray-800"
                title="Cancel edit"
              >
                <X className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={onSubmitEdit}
                disabled={isEditDisabled}
                className="flex h-8 w-8 items-center justify-center rounded-md bg-gray-900 text-white transition hover:bg-black disabled:cursor-not-allowed disabled:opacity-40"
                title="Submit edit"
              >
                <Check className="h-4 w-4" />
              </button>
            </div>
          </div>
        ) : hasContent || isPending ? (
          <div
            className={clsx(
              "rounded-2xl px-4 py-3 text-[13px] leading-6 shadow-sm",
              isPending
                ? "rounded-tl-md border border-gray-200 bg-white text-gray-600"
                : isError
                  ? "rounded-tl-md border border-rose-200 bg-rose-50 text-rose-800"
                  : isAssistant
                    ? "rounded-tl-md border border-gray-200 bg-white text-gray-900"
                    : "ml-auto rounded-tr-md bg-[#1a1a1a] text-white"
            )}
          >
            {hasContent ? (
              <ChatMessageContent content={message.content} />
            ) : (
              <div className="flex items-center gap-1.5 py-1">
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-gray-400" />
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-gray-400 [animation-delay:120ms]" />
                <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-gray-400 [animation-delay:240ms]" />
              </div>
            )}
            {isAssistant && teachingProgress && !isPending && !isError ? (
              <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-gray-200 pt-3 text-[11px] text-gray-500">
                <span>

                  No. {teachingProgress.section_index + 1}/{teachingProgress.section_count}  item
                  {teachingProgress.current_section_title ? `：${teachingProgress.current_section_title}` : ""}
                </span>
                {teachingProgress.has_next_section && onContinueTeaching ? (
                  <button
                    type="button"
                    onClick={onContinueTeaching}
                    className="inline-flex h-7 items-center gap-1 rounded-md border border-gray-200 bg-white px-2.5 font-semibold text-gray-700 transition hover:border-gray-300 hover:text-gray-950"
                  >
                    <ArrowRight className="h-3.5 w-3.5" />

                    Continue to next item
                  </button>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}

        {isAssistant && !isPending && !isError && sourceCitations.length ? (
          <details className="group/source-basis overflow-hidden rounded-xl border border-blue-100 bg-blue-50/40">
            <summary className="flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-blue-700 [&::-webkit-details-marker]:hidden">
              <span>Data basis</span>
              <ChevronDown aria-hidden="true" className="h-3.5 w-3.5 transition-transform group-open/source-basis:rotate-180" />
            </summary>
            <div className="divide-y divide-blue-100">
              {sourceCitations.map((citation, index) => {
                const pageLabel = citation.page_start
                  ? citation.page_end && citation.page_end !== citation.page_start
                    ? `Pages ${citation.page_start}–${citation.page_end}`
                    : `Page ${citation.page_start}`
                  : "Original location";
                const sectionLabel = citation.section_path.filter(Boolean).join(" › ");
                return (
                  <button
                    key={`${citation.evidence_id}:${index}`}
                    type="button"
                    onClick={() => void onOpenSourceCitation?.(citation)}
                    disabled={!onOpenSourceCitation}
                    className="group/citation block w-full px-3 py-2.5 text-left transition hover:bg-blue-50 disabled:cursor-default"
                  >
                    <span className="flex items-start justify-between gap-2">
                      <span className="min-w-0 text-[11px] font-semibold leading-5 text-blue-900">
                        [{sourceCitationDisplayNumber(citation, index)}] {citation.source_title} · {pageLabel}
                      </span>
                      {onOpenSourceCitation ? <MoveUpRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-blue-500" /> : null}
                    </span>
                    {sectionLabel ? <span className="block truncate text-[10px] text-blue-700">{sectionLabel}</span> : null}
                    <span className="mt-1 block max-h-10 overflow-hidden text-[11px] leading-5 text-gray-600">
                      “{selectionPreviewText(citation.excerpt)}”
                    </span>
                  </button>
                );
              })}
            </div>
          </details>
        ) : null}

        {isAssistant && !isPending && !isError && followUpSuggestions.length && onFollowUpSuggestion ? (
          <div className="overflow-hidden rounded-xl border border-gray-200 bg-white">
            <p className="px-3 pb-1 pt-2.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-gray-400">

              OK next
            </p>
            <div className="divide-y divide-gray-100">
              {followUpSuggestions.map((suggestion) => (
                <button
                  key={suggestion}
                  type="button"
                  onClick={() => onFollowUpSuggestion(suggestion)}
                  className="group/suggestion flex w-full items-start gap-2.5 px-3 py-2.5 text-left text-[12px] leading-5 text-gray-700 transition hover:bg-gray-50 hover:text-gray-950"
                >
                  <MoveUpRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-gray-400 transition group-hover/suggestion:text-gray-700" />
                  <span>{suggestion}</span>
                </button>
              ))}
            </div>
          </div>
        ) : null}

        {hasContent && !isEditing ? (
          <div
            className={clsx(
              "flex items-center gap-1 opacity-0 transition group-hover:opacity-100",
              !isAssistant && "justify-end"
            )}
          >
            {!isAssistant && onStartEdit ? (
              <button
                type="button"
                onClick={onStartEdit}
                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
                title="edit this message"
              >
                <PencilLine className="h-3.5 w-3.5" />
              </button>
            ) : null}
            {!isAssistant && onDelete ? (
              <button
                type="button"
                onClick={onDelete}
                disabled={Boolean(deleteDisabledReason)}
                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-gray-400 transition hover:bg-gray-100 hover:text-gray-700 disabled:cursor-not-allowed disabled:opacity-40"
                aria-label={deleteLabel}
                title={deleteDisabledReason || deleteLabel}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            ) : null}
            {isAssistant && onRegenerate ? (
              <button
                type="button"
                onClick={onRegenerate}
                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
                title="Regenerate answer"
                aria-label="Regenerate answer"
              >
                <RefreshCcw className="h-3.5 w-3.5" />
              </button>
            ) : null}
            {isAssistant && onFork ? (
              <button
                type="button"
                onClick={onFork}
                className="inline-flex h-7 w-7 items-center justify-center rounded-md text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
                title="Fork from here"
                aria-label="Fork from here"
              >
                <GitFork className="h-3.5 w-3.5" />
              </button>
            ) : null}
            <button
              type="button"
              onClick={() => void copyMessage()}
              aria-label={copyStatus === "copied" ? "Copied" : copyStatus === "error" ? "Copy failed" : "copy"}
              className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-[11px] font-medium text-gray-400 transition hover:bg-gray-100 hover:text-gray-700"
            >
              {copyStatus === "copied" ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              {copyStatus === "copied" ? "Copied" : copyStatus === "error" ? "Copy failed" : "copy"}
            </button>
          </div>
        ) : null}
      </div>
    </article>
  );
}
