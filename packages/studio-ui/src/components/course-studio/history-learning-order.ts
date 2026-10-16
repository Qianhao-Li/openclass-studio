import type { CommitRecord, Lesson } from "../../types";

/** A learning order references immutable events; their original parents stay untouched. */
export function lessonLearningCommits(lesson: Lesson, throughId?: string | null): CommitRecord[] | null {
  const branch = lesson.history_graph.branches[lesson.history_graph.current_branch];
  if (!branch?.learning_node_ids?.length) return null;
  const byId = new Map(lesson.history_graph.commits.map((commit) => [commit.id, commit]));
  const ids = branch.learning_node_ids;
  if (new Set(ids).size !== ids.length || ids.some((id) => !byId.has(id))) return [];
  const throughIndex = throughId ? ids.indexOf(throughId) : ids.length - 1;
  if (throughIndex < 0) return [];
  return ids.slice(0, throughIndex + 1).map((id) => byId.get(id)!);
}

export function lessonSourceCommits(lesson: Lesson, throughId?: string | null): CommitRecord[] {
  const byId = new Map(lesson.history_graph.commits.map((commit) => [commit.id, commit]));
  let id = throughId ?? lesson.history_graph.branches[lesson.history_graph.current_branch]?.head_commit_id;
  const commits: CommitRecord[] = [];
  const seen = new Set<string>();
  while (id && !seen.has(id)) {
    seen.add(id);
    const commit = byId.get(id);
    if (!commit) break;
    commits.push(commit);
    id = commit.parent_ids[0];
  }
  return commits.reverse();
}

export function lessonPlaybackIdentity(lesson: Lesson): string {
  const branch = lesson.history_graph.branches[lesson.history_graph.current_branch];
  return JSON.stringify([
    lesson.id,
    lesson.history_graph.current_branch,
    branch?.head_commit_id,
    branch?.document_cursor_commit_id,
    branch?.learning_node_ids,
    // A deletion or reordered merge can keep the same HEAD.
    lesson.history_graph.commits.map((commit) => commit.id),
  ]);
}
