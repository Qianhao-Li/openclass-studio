"use client";

import clsx from "clsx";
import { ChevronDown, ChevronRight, FileCode2, Folder, Network, TextQuote } from "lucide-react";
import { useMemo, useState } from "react";

import { createRepositoryNodeSelection } from "./source-reference";
import type {
  RepositoryMapNode,
  RepositoryMapView,
  RepositoryTreeKind,
  SelectionRef,
  SourceIngestionRecord,
} from "../../types";

export function RepositorySourceMap({
  source,
  map,
  onSourceReference,
}: {
  source: SourceIngestionRecord;
  map: RepositoryMapView;
  onSourceReference?: (selection: SelectionRef) => void;
}) {
  const [treeKind, setTreeKind] = useState<RepositoryTreeKind>("project");
  const nodes = treeKind === "project" ? map.project_nodes : map.learning_nodes;
  const roots = nodes.filter((node) => !node.parent_id);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(
    () => new Set(map.project_nodes.filter((node) => !node.parent_id).map((node) => node.id))
  );
  const childCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const node of nodes) {
      if (node.parent_id) counts.set(node.parent_id, (counts.get(node.parent_id) ?? 0) + 1);
    }
    return counts;
  }, [nodes]);
  const nodeById = useMemo(() => new Map(nodes.map((node) => [node.id, node])), [nodes]);
  const visibleNodes = nodes.filter((node) => {
    let parentId = node.parent_id;
    while (parentId) {
      if (!expandedIds.has(parentId)) return false;
      parentId = nodeById.get(parentId)?.parent_id ?? null;
    }
    return true;
  });

  function changeTree(next: RepositoryTreeKind) {
    setTreeKind(next);
    const nextNodes = next === "project" ? map.project_nodes : map.learning_nodes;
    setExpandedIds(new Set(nextNodes.filter((node) => !node.parent_id).map((node) => node.id)));
  }

  return (
    <div>
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex rounded-md border border-blue-100 bg-white p-0.5">
          {(["project", "learning"] as const).map((kind) => (
            <button
              key={kind}
              type="button"
              onClick={() => changeTree(kind)}
              className={clsx(
                "rounded px-2 py-1 text-[11px] font-medium transition",
                treeKind === kind ? "bg-blue-600 text-white" : "text-gray-600 hover:bg-blue-50"
              )}
            >
              {kind === "project" ? "Project structure" : "learning structure"}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-gray-500">

          Analyzed {map.analyzed_file_count}/{map.readable_file_count}  readable files
        </span>
      </div>
      {!nodes.length ? (
        <p className="text-xs leading-5 text-gray-600">
          {treeKind === "learning" ? "The learning structure has not been generated yet; the project structure can still be referenced." : "The project structure is not ready yet."}
        </p>
      ) : (
        <div className="max-h-80 overflow-auto rounded-md border border-blue-100 bg-white py-1">
          {visibleNodes.map((node) => (
            <RepositoryNodeRow
              key={node.id}
              node={node}
              hasChildren={Boolean(childCounts.get(node.id))}
              expanded={expandedIds.has(node.id)}
              onToggle={() =>
                setExpandedIds((current) => {
                  const next = new Set(current);
                  if (next.has(node.id)) next.delete(node.id);
                  else next.add(node.id);
                  return next;
                })
              }
              onReference={
                node.selectable && onSourceReference
                  ? () => onSourceReference(createRepositoryNodeSelection(source, map, node))
                  : undefined
              }
            />
          ))}
        </div>
      )}
      {roots.length && map.warnings.length ? (
        <p className="mt-2 text-[10px] leading-4 text-amber-700">{map.warnings.join("; ")}</p>
      ) : null}
    </div>
  );
}

function RepositoryNodeRow({
  node,
  hasChildren,
  expanded,
  onToggle,
  onReference,
}: {
  node: RepositoryMapNode;
  hasChildren: boolean;
  expanded: boolean;
  onToggle: () => void;
  onReference?: () => void;
}) {
  const Icon = node.node_kind === "file" ? FileCode2 : node.tree_kind === "learning" ? Network : Folder;
  return (
    <div
      className="group flex min-h-7 items-start gap-1 pr-1 text-xs hover:bg-blue-50/60"
      style={{ paddingLeft: `${Math.min(node.level, 12) * 14 + 4}px` }}
    >
      <button
        type="button"
        onClick={onToggle}
        disabled={!hasChildren}
        className="mt-1 flex h-5 w-5 shrink-0 items-center justify-center text-gray-400 disabled:opacity-0"
        aria-label={expanded ? `Collapse ${node.title}` : `Expand ${node.title}`}
      >
        {expanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
      </button>
      <Icon className="mt-1.5 h-3.5 w-3.5 shrink-0 text-blue-600" />
      <div className="min-w-0 flex-1 py-1">
        <p className="break-words leading-5 text-gray-800">{node.title}</p>
        {node.description ? <p className="text-[10px] leading-4 text-gray-500">{node.description}</p> : null}
      </div>
      {onReference ? (
        <button
          type="button"
          onClick={onReference}
          className="mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded text-blue-600 opacity-70 hover:bg-blue-100 group-hover:opacity-100"
          title="Reference this project node"
          aria-label={`Reference project node ${node.title}`}
        >
          <TextQuote className="h-3.5 w-3.5" />
        </button>
      ) : (
        <span className="mt-1.5 text-[9px] text-gray-400">Not verified</span>
      )}
    </div>
  );
}
