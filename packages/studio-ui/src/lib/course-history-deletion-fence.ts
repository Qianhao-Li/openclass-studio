import type { CoursePackage } from "../types";

export type DeletedHistoryNode = { lesson_id: string; node_id: string };
type RemovedNodes = { legacy: Set<string>; lessons: Map<string, Set<string>> };

function declaredDeletion(value: unknown, lessonId: string, nodeId: string, role: string) {
  return Array.isArray(value) && value.some((item: unknown) => {
    if (!item || typeof item !== "object") return false;
    const ref = item as Record<string, unknown>;
    return ref.lesson_id === lessonId && ref.node_id === nodeId && ref.status === "deleted" && ref.role === role;
  });
}

/** Reject late node resurrection while accepting explicit RIDOC deleted references. */
export class CourseHistoryDeletionFence {
  private readonly removed = new Map<string, RemovedNodes>();
  generation = 0;
  private scope = "";

  enterScope(scope: string) {
    if (this.scope === scope) return;
    this.scope = scope;
    this.generation += 1;
  }

  private key(packageId: string, scope: string) { return JSON.stringify([scope, packageId]); }

  record(packageId: string, nodeIds: string[], scope = "", lessonId?: string) {
    if (!nodeIds.length) return;
    const key = this.key(packageId, scope);
    const removed = this.removed.get(key) ?? { legacy: new Set<string>(), lessons: new Map<string, Set<string>>() };
    const ids = lessonId ? removed.lessons.get(lessonId) ?? new Set<string>() : removed.legacy;
    for (const id of nodeIds) ids.add(id);
    if (lessonId) removed.lessons.set(lessonId, ids);
    this.removed.set(key, removed);
    this.generation += 1;
  }

  recordNodes(packageId: string, nodes: DeletedHistoryNode[], scope = "") {
    const lessons = new Map<string, string[]>();
    for (const node of nodes) lessons.set(node.lesson_id, [...lessons.get(node.lesson_id) ?? [], node.node_id]);
    for (const [lessonId, ids] of lessons) this.record(packageId, ids, scope, lessonId);
  }

  contains(packageId: string, nodeId: string, scope = "", lessonId?: string) {
    const removed = this.removed.get(this.key(packageId, scope));
    if (!removed) return false;
    return removed.legacy.has(nodeId) || (lessonId ? removed.lessons.get(lessonId)?.has(nodeId) ?? false
      : [...removed.lessons.values()].some((ids) => ids.has(nodeId)));
  }

  accepts(coursePackage: CoursePackage, scope = "") {
    if (!this.removed.has(this.key(coursePackage.id, scope))) return true;
    return coursePackage.lessons.every((lesson) => {
      const removed = (id: string) => this.contains(coursePackage.id, id, scope, lesson.id);
      return lesson.history_graph.commits.every((commit) =>
        !removed(commit.id) && commit.parent_ids.every((id) =>
          !removed(id) || declaredDeletion(commit.metadata?.node_refs, lesson.id, id, "parent")
        )
      ) && Object.values(lesson.history_graph.branches).every((branch) => {
        const refs = (branch as typeof branch & { node_refs?: unknown }).node_refs;
        return (!removed(branch.head_commit_id) || declaredDeletion(refs, lesson.id, branch.head_commit_id, "head"))
          && (!removed(branch.base_commit_id) || declaredDeletion(refs, lesson.id, branch.base_commit_id, "base"));
      });
    });
  }
}
