const RELATIONS: Record<string, string> = {
  parent: "Following node", learning_context: "Learning context", input: "Input reference", output: "Output reference", source_message: "Original conversation",
  head: "Branch head", base: "Branch base", retained_state: "Retained board", source_head_id: "Merge source", target_head_id: "Merge target",
  base_node_id: "Common base", reference: "Node reference", before: "Board before change", after: "Board after change",
};
export function historyReferenceLabel(relation: string) {
  return relation.split(/,\s*/).map((role) => RELATIONS[role] ?? "Related reference").join(", ");
}
