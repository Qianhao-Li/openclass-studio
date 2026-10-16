/** A persisted sequence alone is not proof of customization: normal turns also store it. */
export function hasCustomHistoryOrder(learningIds: readonly string[], appendedIds: readonly string[]): boolean {
  const included = new Set(learningIds);
  const original = appendedIds.filter((id) => included.has(id));
  return original.length === learningIds.length && original.some((id, index) => id !== learningIds[index]);
}
