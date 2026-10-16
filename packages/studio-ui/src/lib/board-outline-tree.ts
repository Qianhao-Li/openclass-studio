export type OutlineHeadingIdentity = { position: number; depth: number };

export type OutlineEntry<T extends OutlineHeadingIdentity> = {
  heading: T;
  level: number;
  parent: number | null;
  children: number[];
  path: number[];
};

export type OutlineTree<T extends OutlineHeadingIdentity> = {
  roots: number[];
  entries: Map<number, OutlineEntry<T>>;
};

/** A skipped heading level still belongs to the nearest preceding shallower heading. */
export function buildOutlineTree<T extends OutlineHeadingIdentity>(headings: T[]): OutlineTree<T> {
  const roots: number[] = [];
  const entries = new Map<number, OutlineEntry<T>>();
  const ancestors: OutlineEntry<T>[] = [];
  for (const heading of headings) {
    while (ancestors.length && ancestors.at(-1)!.heading.depth >= heading.depth) ancestors.pop();
    const parent = ancestors.at(-1);
    const entry: OutlineEntry<T> = {
      heading,
      level: ancestors.length,
      parent: parent?.heading.position ?? null,
      children: [],
      path: [...(parent?.path ?? []), heading.position],
    };
    entries.set(heading.position, entry);
    (parent?.children ?? roots).push(heading.position);
    ancestors.push(entry);
  }
  return { roots, entries };
}

export function visibleOutlineEntries<T extends OutlineHeadingIdentity>(tree: OutlineTree<T>, openPath: number[]) {
  const visible: OutlineEntry<T>[] = [];
  const visit = (positions: number[]) => {
    for (const position of positions) {
      const entry = tree.entries.get(position)!;
      visible.push(entry);
      if (openPath.includes(position)) visit(entry.children);
    }
  };
  visit(tree.roots);
  return visible;
}

export function visibleOutlineAncestor<T extends OutlineHeadingIdentity>(
  tree: OutlineTree<T>, position: number | null, visible: OutlineEntry<T>[],
) {
  const path = position === null ? [] : tree.entries.get(position)?.path ?? [];
  return [...path].reverse().find((ancestor) => visible.some(({ heading }) => heading.position === ancestor)) ?? null;
}
