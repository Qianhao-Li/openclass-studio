import type { BoardDocument, CoursePackage, RidocHistoryPage } from "../types";
import type { HistoryEditSession, HistoryEditSessionResponse, HistoryEditSessionTransport } from "../types/history-edit";

const HASH = /^[a-f0-9]{64}$/;
const REF = "$history_document_ref";
const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value));

function isDocument(value: unknown): value is BoardDocument {
  return record(value) && typeof value.id === "string" && typeof value.title === "string"
    && typeof value.content_text === "string" && typeof value.content_html === "string"
    && record(value.content_json) && record(value.page_settings);
}

/** Expand only server-issued document references; repeated snapshots share the actual document object. */
export function decodeHistoryEditSession(wire: HistoryEditSessionTransport): HistoryEditSessionResponse {
  if (!record(wire)) throw new Error("The History draft response is invalid.");
  if ("reuse_base_revision" in wire) {
    if (wire.draft_course_package || wire.draft_history || wire.document_table && Object.keys(wire.document_table).length) {
      throw new Error("The clean History draft response is invalid.");
    }
    return wire;
  }
  if (!("document_table" in wire)) return wire;
  const documents = wire.document_table;
  if (!record(documents) || Object.entries(documents).some(([hash, document]) => !HASH.test(hash) || !isDocument(document))) {
    throw new Error("The History draft document table is invalid.");
  }
  function expandDocument(value: unknown): BoardDocument {
    if (isDocument(value)) return value;
    const hash = record(value) ? value[REF] : null;
    if (!record(value) || Object.keys(value).length !== 1 || typeof hash !== "string" || !Object.hasOwn(documents, hash)) {
      throw new Error("A History draft snapshot is missing or invalid. Reload the draft.");
    }
    return documents[hash];
  }
  function expandDocuments(value: unknown) {
    if (!record(value)) throw new Error("The History draft snapshot map is invalid.");
    return Object.fromEntries(Object.entries(value).map(([key, document]) => [key, expandDocument(document)]));
  }
  const { document_table: _documents, ...session } = wire;
  void _documents;
  const course = session.draft_course_package;
  const history = session.draft_history;
  if (!course || !history || !Array.isArray(course.lessons)) throw new Error("The History draft is incomplete. Reload the draft.");
  return { ...session, draft_course_package: { ...course, lessons: course.lessons.map((lesson) => ({ ...lesson,
    board_document: expandDocument(lesson.board_document),
    history_graph: { ...lesson.history_graph,
      commits: lesson.history_graph.commits.map((commit) => ({ ...commit, snapshot: expandDocument(commit.snapshot) })),
      branches: Object.fromEntries(Object.entries(lesson.history_graph.branches).map(([name, branch]) => [name, { ...branch,
        ...(branch.replay_documents ? { replay_documents: expandDocuments(branch.replay_documents) } : {}),
      }])),
    },
  })) }, draft_history: { ...history,
    ...(history.replay_documents ? { replay_documents: expandDocuments(history.replay_documents) } : {}),
    ...(history.document_history ? { document_history: { ...history.document_history,
      actions: history.document_history.actions?.map((action) => action.range_projection?.before_document
        ? { ...action, range_projection: { ...action.range_projection, before_document: expandDocument(action.range_projection.before_document) } }
        : action),
    } } : {}),
  } } as HistoryEditSession;
}

/** Clean reuse is allowed only against the exact already-loaded course and complete History view. */
export function resolveHistoryEditSession(view: HistoryEditSessionResponse, {
  course, history, lessonId, previous,
}: { course: CoursePackage; history?: RidocHistoryPage | null; lessonId: string; previous?: HistoryEditSession | null }): HistoryEditSession | null {
  if (!view.id || view.package_id !== course.id || view.lesson_id !== lessonId || !HASH.test(view.base_ridoc_revision)
      || !Number.isSafeInteger(view.version) || view.version < 1 || view.state !== "editing") {
    throw new Error("The History draft does not match the selected course.");
  }
  if (previous?.id === view.id && (view.version < previous.version || view.base_ridoc_revision !== previous.base_ridoc_revision)) {
    throw new Error("An older History draft response was ignored. Reload the current draft.");
  }
  if (!view.stale && course.ridoc_revision !== view.base_ridoc_revision) {
    throw new Error("The course changed while History was loading. Reload this course before editing History.");
  }
  if ("reuse_base_revision" in view) {
    if (view.reuse_base_revision !== view.base_ridoc_revision || view.dirty || view.pending || view.operation_count !== 0
        || view.deleted_node_ids.length || view.pending_removals?.length || view.stale) {
      throw new Error("The History draft cannot reuse this course revision.");
    }
    if (!history || history.lesson_id !== lessonId || history.ridoc_revision !== view.reuse_base_revision || history.next_cursor) return null;
    const order = view.base_learning_node_ids ?? history.learning_node_ids;
    if (!order || history.nodes.length > 0 && order.length === 0) return null;
    const nodeIds = new Set(history.nodes.map((node) => node.id));
    if (new Set(order).size !== order.length || order.some((id) => !id || !nodeIds.has(id))) {
      throw new Error("The History draft order does not match the loaded course.");
    }
    return { ...view, draft_course_package: course,
      draft_history: view.base_learning_node_ids ? { ...history, learning_node_ids: order } : history };
  }
  if (!view.draft_course_package || view.draft_course_package.id !== view.package_id
      || !view.draft_course_package.lessons.some((lesson) => lesson.id === lessonId)
      || !view.draft_history || view.draft_history.lesson_id !== lessonId
      || view.draft_history.ridoc_revision !== view.base_ridoc_revision) {
    throw new Error("The History draft is incomplete. Reload the draft.");
  }
  return view;
}
