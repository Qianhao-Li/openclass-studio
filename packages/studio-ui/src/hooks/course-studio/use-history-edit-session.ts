"use client";

import { useMemo, useCallback, useEffect, useRef, useState } from "react";
import { ApiRequestError } from "../../lib/api";
import { useStudioApi, useStudioContext } from "../../contexts/studio-api-context";
import { createHistoryEditApi } from "../../lib/history-edit-api";
import { resolveHistoryEditSession } from "../../lib/history-edit-transport";
import { useHistoryStudioLeave } from "./use-history-studio-leave";
import { useWorkspacePrincipal } from "../../contexts/studio-host-context";
import { EMPTY_MERGE_SELECTION, selectMergeMessage } from "../../lib/merge-event-order";
import type { BoardDocument, CommitRecord, CoursePackage, Lesson, RidocHistoryPage } from "../../types";
import type { HistoryEditImpact, HistoryEditMergeDraft, HistoryEditOperation, HistoryEditReview, HistoryEditSession, HistoryEditSessionResponse } from "../../types/history-edit";
import type { CoursePackageApplyOptions } from "./use-course-workspace";
import type { AutoSaveReason } from "./use-board-draft";

type Options = {
  repositoryId?: string | null; coursePackage: CoursePackage | null; lesson: Lesson | null;
  ridocHistory?: RidocHistoryPage | null;
  getCoursePackageSnapshot?: () => CoursePackage | null;
  readOnly: boolean; busyReason: string | null;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  endPreview: () => void;
  applyCoursePackage: (course: CoursePackage, options?: CoursePackageApplyOptions) => unknown;
};
type QueuedOperation = { id: string; lessonId: string; impactToken?: string; operation: HistoryEditOperation; orderGeneration?: number; version?: number; resolve: (ok: boolean) => void };

/** History edits stay in the persistent draft until an explicit, reviewed save. */
export function useHistoryEditSession(options: Options) {
  const api = useStudioApi();
  const studioContext = useStudioContext();
  const historyEditApi = useMemo(() => createHistoryEditApi(api.request), [api]);
  const ownerId = useWorkspacePrincipal()?.id ?? "";
  const scope = JSON.stringify([ownerId, studioContext, options.repositoryId, options.coursePackage?.id]);
  const lessonId = options.lesson?.id;
  const view = JSON.stringify([scope, lessonId]);
  const [sessionState, setSessionState] = useState<{ scope: string; value: HistoryEditSession | null }>({ scope, value: null });
  const session = sessionState.scope === scope ? sessionState.value : null;
  const setSession = useCallback((value: HistoryEditSession | null) => setSessionState({ scope, value }), [scope]);
  const [editingScope, setEditingScope] = useState<string | null>(null);
  const [enteringView, setEnteringView] = useState<string | null>(null);
  const [phaseState, setPhaseState] = useState<{ scope: string; value: "idle" | "loading" | "operating" | "saving" }>({ scope, value: "idle" });
  const phase = enteringView === view ? "loading" : phaseState.scope === scope ? phaseState.value : "idle";
  const setPhase = (value: typeof phaseState.value) => setPhaseState({ scope, value });
  const [messageState, setMessageState] = useState<{ scope: string; value: string | null }>({ scope, value: null });
  const message = messageState.scope === scope ? messageState.value : null;
  const setMessage = useCallback((value: string | null) => setMessageState({ scope, value }), [scope]);
  const [conflictScope, setConflictScope] = useState<string | null>(null);
  const conflict = conflictScope === scope;
  const setConflict = (value: boolean) => setConflictScope(value ? scope : null);
  const [selection, setSelection] = useState(EMPTY_MERGE_SELECTION);
  const [optimisticOrder, setOptimisticOrder] = useState<{ branch: string; ids: string[] } | null>(null);
  const [impactDialog, setImpactDialog] = useState<HistoryEditImpact | null>(null);
  const impactRequest = useRef<{ operation: HistoryEditOperation; impact: HistoryEditImpact; resolve: (ok: boolean) => void } | null>(null);
  const [queuedCount, setQueuedCount] = useState(0);
  const [resolvedView, setResolvedView] = useState<string | null>(null);
  const [mergeDraft, setMergeDraft] = useState<HistoryEditMergeDraft | null>(null);
  const [previewNodeId, setPreviewNodeId] = useState<string | null>(null);
  const [previewBoard, setPreviewBoard] = useState<BoardDocument | null>(null);
  const latest = useRef(options);
  const currentScope = useRef(scope);
  const activeRef = useRef(false);
  const sessionRef = useRef<HistoryEditSession | null>(null);
  const queue = useRef<QueuedOperation[]>([]);
  const draining = useRef<Promise<boolean> | null>(null);
  const closing = useRef(false);
  const exitRequest = useRef<Promise<boolean> | null>(null);
  const mutation = useRef<{ version: number; id: string; token: string; uncertain?: boolean } | null>(null);
  const orderGeneration = useRef(0);
  const recoverySequence = useRef(0);
  const beginning = useRef(false);
  const entryGeneration = useRef(0);
  const pendingKey = `openclass.history.edit.pending:${scope}`;
  useEffect(() => { latest.current = options; });
  const enabled = !options.readOnly && options.coursePackage?.storage_kind === "ridoc";
  const active = editingScope === view;
  const entering = enteringView === view;
  const visible = session && session.package_id === options.coursePackage?.id ? session : null;
  const pending = Boolean(visible?.pending || visible?.dirty || queuedCount || optimisticOrder);
  const checking = enabled && Boolean(lessonId) && resolvedView !== view;
  const stale = Boolean(visible && (visible.stale || conflict || visible.base_ridoc_revision !== options.coursePackage?.ridoc_revision));
  const draftLesson = active || pending ? visible?.draft_course_package.lessons.find((lesson) => lesson.id === lessonId) ?? null : null;
  const branchName = draftLesson?.history_graph.current_branch ?? "";
  const ids = optimisticOrder?.branch === branchName ? optimisticOrder.ids
    : (visible && visible.draft_history.lesson_id === lessonId ? visible.draft_history.learning_node_ids : undefined) ?? draftLesson?.history_graph.branches[branchName]?.learning_node_ids ?? [];
  const disabledReason = options.busyReason ?? (active && stale ? "The course has changed. Your edit draft is preserved. Discard this edit and start again." : active ? visible?.blocked_reason ?? null : null);

  const materialize = useCallback(async (value: HistoryEditSessionResponse, requestedLessonId: string): Promise<HistoryEditSession> => {
    const course = latest.current.getCoursePackageSnapshot?.() ?? latest.current.coursePackage;
    if (!course) throw new Error("The selected course is unavailable.");
    const resolve = (next: HistoryEditSessionResponse) => resolveHistoryEditSession(next, {
      course: latest.current.getCoursePackageSnapshot?.() ?? latest.current.coursePackage ?? course, history: latest.current.ridocHistory,
      lessonId: requestedLessonId, previous: sessionRef.current,
    });
    const resolved = resolve(value);
    if (resolved) return resolved;
    // A partial or not-yet-loaded History cannot stand in for the complete server view.
    const complete = await historyEditApi.active(requestedLessonId, options.repositoryId, false);
    if (!complete || complete.id !== value.id || complete.version < value.version) throw new Error("The History draft changed while it was loading. Try Edit mode again.");
    const result = resolve(complete);
    if (!result) throw new Error("The complete History draft could not be loaded.");
    return result;
  }, [historyEditApi, options.repositoryId]);

  useEffect(() => {
    if (currentScope.current === scope) return;
    currentScope.current = scope; activeRef.current = false; sessionRef.current = null;
    for (const task of queue.current) task.resolve(false);
    queue.current = []; closing.current = false; beginning.current = false; draining.current = null; exitRequest.current = null;
    impactRequest.current?.resolve(false); impactRequest.current = null;
  }, [scope]);
  useEffect(() => {
    const recovery = ++recoverySequence.current;
    entryGeneration.current += 1;
    beginning.current = false;
    activeRef.current = false;
    let cancelled = false;
    if (enabled && lessonId) void historyEditApi.active(lessonId, options.repositoryId).then(async (value) => {
      if (cancelled || recovery !== recoverySequence.current || activeRef.current) return;
      const next = value ? await materialize(value, lessonId) : null;
      if (!cancelled && recovery === recoverySequence.current && !activeRef.current) {
        sessionRef.current = next; setSession(next); setResolvedView(view);
        try {
          const saved = JSON.parse(localStorage.getItem(pendingKey) ?? "null") as { sessionId: string; tasks: QueuedOperation[] } | null;
          if (next && saved?.sessionId === next.id && Array.isArray(saved.tasks)) {
            queue.current = saved.tasks.map((task) => ({ ...task, lessonId: task.lessonId ?? next.lesson_id, resolve: () => undefined }));
            setQueuedCount(queue.current.length);
          }
        } catch { setMessage("Could not recover the pending operation. Check your History draft."); }
      }
    }).catch((error) => { if (!cancelled && recovery === recoverySequence.current) { setMessage(error instanceof Error ? error.message : "Could not load the unfinished edit."); setResolvedView(view); } });
    return () => { cancelled = true; activeRef.current = false; };
  }, [historyEditApi, scope, view, enabled, lessonId, options.repositoryId, pendingKey, setSession, setMessage, materialize]);


  function install(next: HistoryEditSession) {
    recoverySequence.current += 1; setResolvedView(view);
    sessionRef.current = next; setSession(next); mutation.current = null;
    const lesson = next.draft_course_package.lessons.find((item) => item.id === latest.current.lesson?.id);
    if (lesson) { setPreviewBoard(lesson.board_document); setPreviewNodeId(null); }
  }
  function persistQueue() {
    try {
      if (queue.current.length && sessionRef.current) localStorage.setItem(pendingKey, JSON.stringify({ sessionId: sessionRef.current.id,
        tasks: queue.current.map(({ id, operation, version, lessonId, impactToken }) => ({ id, operation, version, lessonId, impactToken })) }));
      else localStorage.removeItem(pendingKey);
    } catch { setMessage("The browser cannot preserve the pending operation. Wait for the draft to save before closing the page."); }
  }
  function restoreQueue(next: HistoryEditSession) {
    try {
      const saved = JSON.parse(localStorage.getItem(pendingKey) ?? "null") as { sessionId: string; tasks: Array<{ id: string; operation: HistoryEditOperation; version?: number; lessonId?: string; impactToken?: string }> } | null;
      if (saved?.sessionId === next.id && Array.isArray(saved.tasks)) {
        queue.current = saved.tasks.map((task) => ({ ...task, lessonId: task.lessonId ?? next.lesson_id, resolve: () => undefined }));
        setQueuedCount(queue.current.length);
      }
      return true;
    } catch { setMessage("Could not recover the pending operation. Check the edit draft before leaving."); return false; }
  }
  async function begin() {
    if (!enabled || options.busyReason || phase !== "idle" || !options.lesson || beginning.current) return false;
    const entry = ++entryGeneration.current;
    const requestedLessonId = options.lesson.id;
    const requestedPackageId = options.coursePackage?.id;
    const isCurrent = () => entry === entryGeneration.current && currentScope.current === scope
      && latest.current.lesson?.id === requestedLessonId
      && (latest.current.getCoursePackageSnapshot?.() ?? latest.current.coursePackage)?.id === requestedPackageId;
    beginning.current = true; recoverySequence.current += 1;
    setEnteringView(view); setMessage(null);
    try {
      if (!(await options.flushAutoSave("preview"))) {
        if (isCurrent()) setMessage("The board could not be saved. Resolve the save error before editing History.");
        return false;
      }
      if (!isCurrent()) return false;
      options.endPreview();
      const course = latest.current.getCoursePackageSnapshot?.() ?? latest.current.coursePackage;
      if (!course?.ridoc_revision) throw new Error("Reload this course before editing History.");
      const response = await historyEditApi.begin(requestedLessonId, course.ridoc_revision, options.repositoryId);
      if (!isCurrent()) return false;
      let next = await materialize(response, requestedLessonId);
      if (!isCurrent()) return false;
      const recoveredQueue = restoreQueue(next);
      // An untouched paused session can outlive later course writes. Replace only
      // that empty session; recorded or unacknowledged operations keep their base.
      if (next.stale && !next.dirty && !next.pending && next.operation_count === 0 && recoveredQueue && !queue.current.length) {
        await historyEditApi.discard(requestedLessonId, next.id, next.version, options.repositoryId);
        if (!isCurrent()) return false;
        sessionRef.current = null; setSession(null);
        const restarted = await historyEditApi.begin(requestedLessonId, course.ridoc_revision, options.repositoryId);
        if (!isCurrent()) return false;
        next = await materialize(restarted, requestedLessonId);
        if (!isCurrent()) return false;
      }
      activeRef.current = true; setEditingScope(view); setConflict(false); setOptimisticOrder(null); setSelection(EMPTY_MERGE_SELECTION);
      install(next);
      if (queue.current.length && !next.stale) void drain();
      return true;
    } catch (error) { if (isCurrent()) setMessage(error instanceof Error ? error.message : "Could not enter edit mode."); return false; }
    finally { if (isCurrent()) { beginning.current = false; setEnteringView(null); } }
  }
  function cancelBegin() {
    if (!entering || !beginning.current) return;
    entryGeneration.current += 1; recoverySequence.current += 1;
    beginning.current = false; setEnteringView(null); setResolvedView(view);
    setMessage("History preparation cancelled.");
  }
  function drain(): Promise<boolean> {
    if (draining.current) return draining.current;
    const operationScope = scope;
    const work = (async () => {
      if (!closing.current) setPhase("operating");
      while (queue.current.length) {
        const task = queue.current[0];
        const current = sessionRef.current;
        if (!current || currentScope.current !== operationScope) return false;
        task.version ??= current.version;
        persistQueue();
        try {
          const response = await historyEditApi.operate(task.lessonId, current.id, task.version, task.id, task.operation, options.repositoryId, task.impactToken);
          if (currentScope.current !== operationScope || sessionRef.current?.id !== current.id) return false;
          const next = await materialize(response, task.lessonId);
          if (currentScope.current !== operationScope || sessionRef.current?.id !== current.id) return false;
          install(next); queue.current.shift(); setQueuedCount(queue.current.length); persistQueue(); task.resolve(true);
          if (task.orderGeneration === orderGeneration.current || task.operation.kind !== "reorder_messages") setOptimisticOrder(null);
        } catch (error) {
          if (currentScope.current !== operationScope || sessionRef.current?.id !== current.id) return false;
          if (error instanceof ApiRequestError && [400, 422].includes(error.status)) {
            queue.current.shift(); setQueuedCount(queue.current.length); persistQueue(); task.resolve(false);
            if (task.operation.kind === "reorder_messages") setOptimisticOrder(null);
            setMessage(`The action was not applied. Your previous edit draft is preserved: ${error.message}`);
            continue;
          }
          if (error instanceof ApiRequestError && error.status === 409) setConflict(true);
          setMessage(`Edit draft preserved: ${error instanceof Error ? error.message : "The operation failed. Please try again."}`);
          task.resolve(false); return false;
        }
      }
      const currentLessonId = latest.current.lesson?.id;
      if (sessionRef.current && currentLessonId && sessionRef.current.lesson_id !== currentLessonId && currentScope.current === operationScope) {
        const refreshed = await historyEditApi.active(currentLessonId, options.repositoryId);
        if (refreshed && currentScope.current === operationScope) {
          const next = await materialize(refreshed, currentLessonId);
          if (currentScope.current === operationScope) install(next);
        }
      }
      return true;
    })();
    draining.current = work;
    const settled = () => { if (draining.current === work) draining.current = null; if (!closing.current && currentScope.current === operationScope) setPhase("idle"); };
    void work.then(settled, settled);
    return work;
  }
  function operate(operation: HistoryEditOperation, generation?: number, impactToken?: string): Promise<boolean> {
    if (!activeRef.current || closing.current || stale || options.busyReason || !sessionRef.current) return Promise.resolve(false);
    setMessage(null);
    return new Promise((resolve) => {
      const last = queue.current.at(-1);
      if (operation.kind === "reorder_messages" && last?.operation.kind === "reorder_messages" && last.version === undefined && last.lessonId === lessonId && last.operation.branch_name === operation.branch_name) {
        queue.current.pop(); last.resolve(true);
      }
      queue.current.push({ id: crypto.randomUUID(), lessonId: lessonId!, impactToken, operation, orderGeneration: generation, resolve }); setQueuedCount(queue.current.length); persistQueue(); void drain();
    });
  }
  function change(nextIds: string[]) {
    if (!activeRef.current || closing.current || stale || options.busyReason) return;
    const generation = ++orderGeneration.current;
    setOptimisticOrder({ branch: branchName, ids: nextIds });
    void operate({ kind: "reorder_messages", branch_name: branchName, ordered_node_ids: nextIds }, generation);
  }
  function select(id: string, visualIds: string[], modifiers: { range?: boolean; toggle?: boolean } = {}) {
    if (!activeRef.current || closing.current || stale) return;
    setSelection((current) => selectMergeMessage(current, id, "history", visualIds, modifiers));
  }
  function finishLocal() {
    recoverySequence.current += 1; setResolvedView(view);
    activeRef.current = false; setEditingScope(null); sessionRef.current = null; setSession(null);
    for (const task of queue.current) task.resolve(false);
    queue.current = []; setQueuedCount(0); setOptimisticOrder(null); setSelection(EMPTY_MERGE_SELECTION); setMergeDraft(null); setImpactDialog(null); impactRequest.current?.resolve(false); impactRequest.current = null; setPreviewNodeId(null); setConflict(false);
    try { localStorage.removeItem(pendingKey); } catch { /* A confirmed server result remains authoritative. */ }
  }
  function commit(review: HistoryEditReview): Promise<boolean> {
    if (exitRequest.current) return exitRequest.current;
    if (!sessionRef.current) return Promise.resolve(true);
    if (stale || visible?.blocked_reason) { setMessage(disabledReason ?? "The course has changed. The edit could not be saved."); return Promise.resolve(false); }
    closing.current = true; setPhase("saving"); setMessage(null);
    const expectedSession = sessionRef.current?.id;
    const work = (async () => {
      try {
        const current = sessionRef.current;
        if (!current || currentScope.current !== scope || current.id !== expectedSession || current.version !== review.version) return false;
        if (mutation.current?.version !== current.version || mutation.current.token !== review.confirmation_token) mutation.current = { version: current.version, id: crypto.randomUUID(), token: review.confirmation_token };
        const result = await historyEditApi.commit(current.lesson_id, current.id, current.version, mutation.current.id, options.repositoryId, review.confirmation_token);
        if (currentScope.current !== scope || sessionRef.current?.id !== expectedSession) return false;
        finishLocal();
        latest.current.endPreview();
        latest.current.applyCoursePackage(result.course_package, { activeLessonId: latest.current.lesson?.id ?? current.lesson_id, rebuildMessageLessonIds: current.affected_lesson_ids ?? result.course_package.lessons.map((lesson) => lesson.id), deletedHistoryNodes: result.removed_nodes, deletedHistoryNodeIds: result.removed_nodes ? undefined : result.deleted_node_ids });
        setMessage("History changes saved.");
        return true;
      } catch (error) {
        if (currentScope.current !== scope || sessionRef.current?.id !== expectedSession) return false;
        if (mutation.current && (!(error instanceof ApiRequestError) || error.status >= 500)) mutation.current.uncertain = true;
        if (error instanceof ApiRequestError && error.status === 409) setConflict(true);
        setMessage(`Saving failed. Your edit draft is preserved: ${error instanceof Error ? error.message : "Please try again."}`); return false;
      } finally { if (currentScope.current === scope && (!sessionRef.current || sessionRef.current.id === expectedSession)) { closing.current = false; exitRequest.current = null; setPhase("idle"); } }
    })();
    exitRequest.current = work;
    return work;
  }
  async function discard({ leaving = false }: { leaving?: boolean } = {}) {
    if (closing.current || beginning.current) return false;
    closing.current = true; recoverySequence.current += 1; setPhase("saving"); setMessage(null);
    let expectedSession = sessionRef.current?.id;
    const currentContext = () => currentScope.current === scope && sessionRef.current?.id === expectedSession;
    try {
      await draining.current;
      if (!currentContext()) return false;
      let current = sessionRef.current;
      if (!current) {
        // A failed initial read does not prove there is no persisted draft.
        if (!lessonId) return false;
        const response = await historyEditApi.active(lessonId, options.repositoryId);
        if (!currentContext()) return false;
        current = response ? await materialize(response, lessonId) : null;
        if (!currentContext()) return false;
        if (!current) { finishLocal(); return true; }
        install(current); expectedSession = current.id;
      }
      await historyEditApi.discard(current.lesson_id, current.id, current.version, options.repositoryId);
      if (!currentContext()) return false;
      finishLocal(); latest.current.endPreview();
      // An acknowledged discard is enough to leave; reloading the departing view must not block it.
      if (!leaving) {
        try {
          const course = await api.getCoursePackage(options.repositoryId, current.lesson_id);
          if (currentScope.current !== scope) return false;
          latest.current.applyCoursePackage(course, { activeLessonId: current.lesson_id });
        } catch (error) {
          if (currentScope.current === scope) setMessage(`Changes were discarded, but the course could not refresh. Reload the page: ${error instanceof Error ? error.message : "Please try again."}`);
        }
      }
      return currentScope.current === scope;
    } catch (error) { if (currentContext()) setMessage(error instanceof Error ? error.message : "Could not discard the edit. Please try again."); return false; }
    finally { if (currentScope.current === scope && (!sessionRef.current || sessionRef.current.id === expectedSession)) { closing.current = false; setPhase("idle"); } }
  }
  async function startMerge(source: string) {
    if (!activeRef.current || stale || closing.current || !(await drain()) || currentScope.current !== scope) return;
    const current = sessionRef.current;
    if (!current) return;
    setPhase("loading");
    try {
      const plan = await historyEditApi.mergePlan(current.lesson_id, current.id, source, options.repositoryId);
      if (currentScope.current !== scope || sessionRef.current?.id !== current.id || sessionRef.current.version !== current.version) return;
      setMergeDraft({ source, target: current.draft_history.current_branch, plan });
    } catch (error) { if (currentScope.current === scope && sessionRef.current?.id === current.id) setMessage(error instanceof Error ? error.message : "Could not preview the merge."); }
    finally { if (currentScope.current === scope && sessionRef.current?.id === current.id) setPhase("idle"); }
  }
  async function confirmMerge() {
    if (!mergeDraft || mergeDraft.plan.blockers.length) return;
    if (await operate({ kind: "merge_branch", source_branch_name: mergeDraft.source, target_branch_name: mergeDraft.target, ordered_node_ids: mergeDraft.plan.ordered_node_ids })) setMergeDraft(null);
  }
  function preview(commit: CommitRecord) {
    if ((!activeRef.current && !pending) || closing.current) return;
    setPreviewNodeId(commit.id); setPreviewBoard(commit.snapshot);
  }
  async function pause(): Promise<boolean> {
    if (closing.current) return exitRequest.current ?? false;
    cancelImpact();
    if (!(await drain())) return false;
    const current = sessionRef.current;
    if (!current) { activeRef.current = false; setEditingScope(null); return true; }
    const expectedSession = current.id;
    closing.current = true; setPhase("saving");
    try {
      const requestedLessonId = lessonId ?? current.lesson_id;
      const response = await historyEditApi.pause(requestedLessonId, current.id, current.version, options.repositoryId);
      if (currentScope.current !== scope || sessionRef.current?.id !== expectedSession) return false;
      const next = await materialize(response, requestedLessonId);
      if (currentScope.current !== scope || sessionRef.current?.id !== expectedSession) return false;
      activeRef.current = false; setEditingScope(null); setOptimisticOrder(null); setSelection(EMPTY_MERGE_SELECTION);
      install(next); setMergeDraft(null); setImpactDialog(null); return true;
    } catch (error) { if (currentScope.current === scope) setMessage(error instanceof Error ? error.message : "Could not save the edit draft. Please try again."); return false; }
    finally { if (currentScope.current === scope) { closing.current = false; setPhase("idle"); } }
  }
  async function prepareLeave(intent: "leave" | "save", isCurrent: () => boolean): Promise<HistoryEditReview | null> {
    if (!enabled || !lessonId || !isCurrent()) return null;
    const drained = await drain();
    if (!isCurrent()) return null;
    if (!drained) throw new Error("Draft operations are still pending. Check them and try again.");
    const previous = sessionRef.current;
    const uncertain = mutation.current?.uncertain;
    const currentView = await historyEditApi.active(lessonId, options.repositoryId);
    if (!isCurrent()) return null;
    if (currentScope.current !== scope) throw new Error("The current course has changed.");
    if (currentView) {
      const next = await materialize(currentView, lessonId);
      if (!isCurrent()) return null;
      install(next); setConflict(false);
      if (!queue.current.length) restoreQueue(next);
    } else if (previous && uncertain) {
      // A lost commit response can leave a completed session; reconcile its authoritative course.
      const course = await api.getCoursePackage(options.repositoryId, lessonId);
      if (!isCurrent()) return null;
      if (currentScope.current !== scope) throw new Error("The current course has changed.");
      finishLocal(); latest.current.endPreview(); latest.current.applyCoursePackage(course, { activeLessonId: lessonId });
      return null;
    } else if (!currentView) {
      if (previous?.dirty || queue.current.length) throw new Error("The edit draft was closed in another window. Check the current course.");
      finishLocal(); return null;
    }
    if (intent === "leave" && !(await pause())) throw new Error("The draft has not finished saving. Check it and try again.");
    if (!isCurrent()) return null;
    const current = sessionRef.current;
    if (!current) return null;
    if (!current.dirty && !current.pending) {
      await historyEditApi.discard(lessonId, current.id, current.version, options.repositoryId);
      if (!isCurrent()) return null;
      if (currentScope.current !== scope) throw new Error("The current course has changed.");
      finishLocal(); latest.current.endPreview(); return null;
    }
    return historyEditApi.review(lessonId, current.id, current.version, options.repositoryId);
  }
  async function markForRemoval(operation: HistoryEditOperation, target: string | string[]): Promise<boolean> {
    if (!activeRef.current || stale || closing.current || !(await drain())) return false;
    const current = sessionRef.current;
    if (!current || !lessonId) return false;
    setPhase("loading");
    try {
      const impact = await historyEditApi.impact(lessonId, current.id, current.version, target, options.repositoryId);
      if (currentScope.current !== scope || sessionRef.current?.id !== current.id || sessionRef.current.version !== current.version) return false;
      if (!impact.requires_confirmation && !impact.referrers.length) return await operate(operation, undefined, impact.impact_token);
      return await new Promise<boolean>((resolve) => { impactRequest.current = { impact, operation, resolve }; setImpactDialog(impact); setPhase("idle"); });
    } catch (error) {
      if (currentScope.current === scope) {
        if (error instanceof ApiRequestError && error.status === 409) setConflict(true);
        setMessage(error instanceof Error ? error.message : "Could not review the effect on nodes.");
      }
      return false;
    }
    finally { if (currentScope.current === scope) setPhase("idle"); }
  }
  async function confirmImpact() {
    const request = impactRequest.current;
    if (!request) return;
    impactRequest.current = null;
    const ok = await operate(request.operation, undefined, request.impact.impact_token);
    request.resolve(ok); setImpactDialog(null);
  }
  function cancelImpact() { impactRequest.current?.resolve(false); impactRequest.current = null; setImpactDialog(null); }
  function stepActions(direction: "undo" | "redo", targets: string[]): Promise<boolean> {
    // Freeze this click's targets. Applying them must never cascade into newly exposed actions.
    const targetNodeIds = [...new Set(targets)];
    if (!targetNodeIds.length) return Promise.resolve(false);
    const operation: HistoryEditOperation = { kind: "step_document_actions", branch_name: branchName, direction, target_node_ids: targetNodeIds };
    return direction === "undo" ? markForRemoval(operation, targetNodeIds) : operate(operation);
  }
  const leave = useHistoryStudioLeave({ scope, pending: checking || pending || active || phase !== "idle", warnOnUnload: pending,
    interceptBrowser: studioContext.kind !== "temporary", prepare: prepareLeave, commit, discard: () => discard({ leaving: true }) });
  return {
    active, enabled, entering, dirty: pending, pending, checking, lockOfficialWrites: checking || active || pending || phase !== "idle", stale, phase, message, disabledReason,
    session: visible, draftHistory: (active || pending) && visible && visible.draft_history.lesson_id === lessonId ? visible.draft_history : null, draftLesson,
    draft: active && draftLesson ? { lesson: draftLesson, ids } : null, selectedIds: selection.ids,
    recovery: !active && Boolean(visible), previewNodeId: previewNodeId, impactDialog: active ? impactDialog : null, mergeDraft: active ? mergeDraft : null,
    displayedDocument: active || pending ? (previewBoard?.id === draftLesson?.board_document.id ? previewBoard : draftLesson?.board_document) ?? null : null,
    previewCommit: draftLesson?.history_graph.commits.find((commit) => commit.id === previewNodeId) ?? null,
    resetPreview: () => { setPreviewNodeId(null); setPreviewBoard(draftLesson?.board_document ?? null); },
    begin, cancelBegin, exit: pause, pause, save: leave.requestSave, discard, guardNavigation: pause, requestLeave: leave.requestLeave, leave, change, select, preview,
    clearSelection: () => setSelection(EMPTY_MERGE_SELECTION),
    stepActions,
    step: (direction: "undo" | "redo", target: string) => stepActions(direction, [target]),
    askDelete: (id: string) => { void markForRemoval({ kind: "delete_message", node_id: id }, id); },
    restoreMessage: (id: string) => operate({ kind: "restore_message", node_id: id }),
    confirmImpact, cancelImpact,
    createBranch: (name: string, fromNode?: string) => operate({ kind: "create_branch", name: name.trim() || `branch-${Date.now()}`, ...(fromNode ? { from_node_id: fromNode } : {}) }),
    checkout: (name: string) => operate({ kind: "checkout_branch", name }),
    startMerge, confirmMerge, cancelMerge: () => setMergeDraft(null),
    changeMergeOrder: (nextIds: string[]) => setMergeDraft((current) => current ? { ...current, plan: { ...current.plan, ordered_node_ids: nextIds } } : current),
  };
}
export type HistoryEditController = ReturnType<typeof useHistoryEditSession>;
