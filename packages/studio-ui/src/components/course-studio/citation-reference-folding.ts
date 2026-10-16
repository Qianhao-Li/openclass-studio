import { Plugin, PluginKey, TextSelection, type EditorState } from "@tiptap/pm/state";
import { Decoration, DecorationSet, type EditorView } from "@tiptap/pm/view";
import { buildCitationReferenceGroups, citationGroupRange, type CitationReferenceGroup } from "./citation-reference-groups";

type FoldingState = { groups: CitationReferenceGroup[]; expanded: Set<string> };
type FoldAction = { key: string; expanded: boolean };
const foldingKey = new PluginKey<FoldingState>("citationReferenceFolding");

function visibleCaretForCollapse(state: EditorState, current: FoldingState, group: CitationReferenceGroup) {
  const hidden = current.groups.filter((item) => item.key === group.key || !current.expanded.has(item.key))
    .flatMap((item) => item.members);
  let before: number | null = null;
  let after: number | null = null;
  state.doc.descendants((node, pos) => {
    if (!node.isTextblock || hidden.some((member) => pos >= member.from && pos < member.to)) return;
    if (pos < group.members[0].from) before = pos + node.nodeSize - 1;
    else if (after === null) after = pos + 1;
  });
  return before ?? after;
}

export function expandCitationReferenceGroup(state: EditorState, position: number) {
  const transaction = state.tr;
  const current = foldingKey.getState(state);
  const group = current?.groups.find((candidate) => candidate.members.some((member) => member.from === position));
  if (group && !current?.expanded.has(group.key)) {
    transaction.setMeta(foldingKey, { key: group.key, expanded: true } satisfies FoldAction);
  }
  return transaction.setMeta("addToHistory", false);
}

function groupWidget(view: EditorView, group: CitationReferenceGroup, expanded: boolean) {
  const container = document.createElement("div");
  container.className = "word-editor__citation-group";
  container.contentEditable = "false";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "word-editor__citation-group-toggle";
  button.dataset.citationGroupKey = group.key;
  button.dataset.citationCount = String(group.members.length);
  button.setAttribute("aria-expanded", String(expanded));
  button.setAttribute("aria-controls", group.members.map((member) => member.anchorId).join(" "));
  const range = citationGroupRange(group);
  const count = `${group.members.length} references`;
  button.setAttribute("aria-label", `${expanded ? "Collapse" : "Expand"} references: ${group.identity}; ${range ? `${range}; ` : ""}${count}`);
  button.title = "Expand to see the full source locations and original reference numbers.";
  const arrow = document.createElement("span");
  arrow.className = "word-editor__citation-group-arrow";
  arrow.setAttribute("aria-hidden", "true");
  arrow.textContent = expanded ? "▾" : "▸";
  const title = document.createElement("span");
  title.className = "word-editor__citation-group-title";
  title.textContent = group.identity;
  const span = document.createElement("span");
  span.className = "word-editor__citation-group-range";
  span.textContent = range;
  const total = document.createElement("span");
  total.className = "word-editor__citation-group-count";
  total.textContent = count;
  button.append(arrow, title);
  if (range) button.append(span);
  button.append(total);
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (view.isDestroyed) return;
    const current = foldingKey.getState(view.state);
    const liveGroup = current?.groups.find((item) => item.key === group.key);
    if (!current || !liveGroup) return;
    const nextExpanded = !current.expanded.has(group.key);
    const transaction = view.state.tr;
    const { from, to } = view.state.selection;
    if (!nextExpanded && liveGroup.members.some((member) => from < member.to && to > member.from)) {
      const caret = visibleCaretForCollapse(view.state, current, liveGroup);
      // A references-only document may have no visible legal caret position.
      // Keep it open instead of leaving an invisible editing/AI selection.
      if (caret === null) return;
      transaction.setSelection(TextSelection.create(view.state.doc, caret));
    }
    view.dispatch(transaction.setMeta(foldingKey, { key: group.key, expanded: nextExpanded } satisfies FoldAction)
      .setMeta("addToHistory", false));
    // The widget may be replaced when its expanded state changes. Keep native
    // keyboard focus on the replacement without selecting hidden document text.
    Array.from(view.dom.querySelectorAll<HTMLButtonElement>(".word-editor__citation-group-toggle"))
      .find((item) => item.dataset.citationGroupKey === group.key)?.focus({ preventScroll: true });
  });
  container.append(button);
  return container;
}

export function citationReferenceFoldingPlugin() {
  return new Plugin<FoldingState>({
    key: foldingKey,
    state: {
      init: (_config, state) => ({ groups: buildCitationReferenceGroups(state.doc), expanded: new Set() }),
      apply(transaction, current) {
        const groups = transaction.docChanged ? buildCitationReferenceGroups(transaction.doc) : current.groups;
        const keys = new Set(groups.map((group) => group.key));
        const expanded = new Set([...current.expanded].filter((key) => keys.has(key)));
        const action = transaction.getMeta(foldingKey) as FoldAction | undefined;
        if (action && keys.has(action.key)) {
          if (action.expanded) expanded.add(action.key);
          else expanded.delete(action.key);
        } else if (transaction.selectionSet) {
          const { from, to } = transaction.selection;
          for (const group of groups) {
            if (group.members.some((member) => from < member.to && to > member.from)) expanded.add(group.key);
          }
        }
        return { groups, expanded };
      },
    },
    props: {
      decorations(state) {
        const current = foldingKey.getState(state);
        if (!current) return DecorationSet.empty;
        const decorations: Decoration[] = [];
        for (const group of current.groups) {
          const expanded = current.expanded.has(group.key);
          decorations.push(Decoration.widget(group.members[0].from,
            (view) => groupWidget(view, group, expanded), {
              key: `${group.key}:${expanded}:${citationGroupRange(group)}:${group.members.map((member) => member.label).join(",")}`,
              side: -1, ignoreSelection: true, stopEvent: () => true,
            }));
          if (!expanded) {
            for (const member of group.members) decorations.push(Decoration.node(member.from, member.to, {
              class: "word-editor__citation-reference--folded", "data-citation-folded": "true",
            }));
          }
        }
        return DecorationSet.create(state.doc, decorations);
      },
    },
  });
}
