"use client";

import type { Editor, EditorEvents } from "@tiptap/core";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type RefObject } from "react";
import { ChevronRight } from "lucide-react";
import { buildOutlineTree, visibleOutlineAncestor, visibleOutlineEntries, type OutlineEntry } from "../../lib/board-outline-tree";
import styles from "./board-document-outline.module.css";

type Heading = { position: number; depth: number; label: string; element: HTMLElement };
type Outline = { headings: Heading[]; active: number | null; width: number };

function readHeadings(editor: Editor): Heading[] {
  const headings: Heading[] = [];
  editor.state.doc.descendants((node, position) => {
    if (node.type.name !== "heading") return;
    const element = editor.view.nodeDOM(position);
    const label = node.textBetween(0, node.content.size, " ", (leaf) => String(leaf.attrs.latex ?? leaf.attrs.alt ?? ""))
      .replace(/\s+/g, " ").trim();
    if (label && element instanceof HTMLElement) {
      headings.push({ position, label, depth: Number(node.attrs.level) || 1, element });
    }
    return false;
  });
  return headings;
}

function useDocumentOutline(editor: Editor | null, viewportRef: RefObject<HTMLDivElement | null>): Outline {
  const [outline, setOutline] = useState<Outline>({ headings: [], active: null, width: 0 });
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!editor || editor.isDestroyed || !viewport) return;
    let headings: Heading[] = [];
    let needsHeadings = true;
    let frame: number | null = null;
    const measure = () => {
      frame = null;
      if (editor.isDestroyed) return;
      if (needsHeadings) {
        headings = readHeadings(editor);
        needsHeadings = false;
      }
      const visible = headings.filter(({ element }) => element.isConnected && element.getClientRects().length > 0);
      const readingLine = viewport.getBoundingClientRect().top + Math.min(80, viewport.clientHeight / 4);
      let active: number | null = visible[0]?.position ?? null;
      for (const heading of visible) {
        if (heading.element.getBoundingClientRect().top <= readingLine) active = heading.position;
      }
      if (viewport.scrollTop > 1 && viewport.scrollTop + viewport.clientHeight >= viewport.scrollHeight - 2) {
        active = visible.at(-1)?.position ?? null;
      }
      const width = viewport.clientWidth;
      setOutline((previous) => previous.headings === headings && previous.active === active && previous.width === width
        ? previous : { headings, active, width });
    };
    const schedule = () => { frame ??= requestAnimationFrame(measure); };
    const onTransaction = ({ transaction }: EditorEvents["transaction"]) => {
      // History and course hydration also replace content with emitUpdate:false.
      if (transaction.docChanged) { needsHeadings = true; schedule(); }
    };
    const resize = new ResizeObserver(schedule);
    resize.observe(viewport);
    resize.observe(editor.view.dom);
    const paper = editor.view.dom.closest(".word-editor__page");
    if (paper) resize.observe(paper);
    // CSS zoom changes visual positions without changing editor node sizes.
    const pageStyle = new MutationObserver(schedule);
    if (paper) pageStyle.observe(paper, { attributes: true, attributeFilter: ["style", "class"] });
    editor.on("transaction", onTransaction);
    viewport.addEventListener("scroll", schedule, { passive: true });
    viewport.addEventListener("load", schedule, true);
    window.addEventListener("resize", schedule);
    schedule();
    return () => {
      if (frame !== null) cancelAnimationFrame(frame);
      editor.off("transaction", onTransaction);
      resize.disconnect();
      pageStyle.disconnect();
      viewport.removeEventListener("scroll", schedule);
      viewport.removeEventListener("load", schedule, true);
      window.removeEventListener("resize", schedule);
    };
  }, [editor, viewportRef]);
  return outline;
}

function revealItem(container: HTMLElement, item: HTMLElement) {
  const bounds = container.getBoundingClientRect();
  const target = item.getBoundingClientRect();
  if (target.top < bounds.top) container.scrollTop += target.top - bounds.top;
  else if (target.bottom > bounds.bottom) container.scrollTop += target.bottom - bounds.bottom;
}

const HOVER_DELAY = 350;

export function BoardDocumentOutline({ editor, scrollContainerRef }: {
  editor: Editor | null;
  scrollContainerRef: RefObject<HTMLDivElement | null>;
}) {
  const { headings, active, width } = useDocumentOutline(editor, scrollContainerRef);
  const tree = useMemo(() => buildOutlineTree(headings), [headings]);
  const [expanded, setExpanded] = useState(false);
  const [focused, setFocused] = useState<number | null>(null);
  const [branch, setBranch] = useState<{ source: Heading[]; path: number[]; rows: number } | null>(null);
  // Document transactions invalidate positional identities, including pending hovers.
  const openPath = branch?.source === headings ? branch.path : [];
  const roots = tree.roots.map((position) => tree.entries.get(position)!);
  const visible = visibleOutlineEntries(tree, openPath);
  const activeRoot = visibleOutlineAncestor(tree, active, roots);
  const activeVisible = visibleOutlineAncestor(tree, active, visible);
  const navRef = useRef<HTMLElement>(null);
  const railRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLOListElement>(null);
  const wasExpanded = useRef(false);
  const keyboardMode = useRef(false);
  const hoverTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hoverTarget = useRef<number | null>(null);
  const lastPointer = useRef<{ x: number; y: number } | null>(null);
  const panelId = useId();

  const cancelHover = useCallback(() => {
    if (hoverTimer.current !== null) clearTimeout(hoverTimer.current);
    hoverTimer.current = null;
    hoverTarget.current = null;
  }, []);
  const closeOutline = useCallback(() => {
    cancelHover();
    lastPointer.current = null;
    setExpanded(false);
    setBranch(null);
    setFocused(null);
  }, [cancelHover]);

  useEffect(() => cancelHover, [headings, cancelHover]);
  useEffect(() => {
    const justOpened = expanded && !wasExpanded.current;
    wasExpanded.current = expanded;
    const rail = railRef.current;
    const marker = rail?.querySelector<HTMLElement>('[data-active="true"]');
    if (rail && marker) revealItem(rail, marker);
    const list = listRef.current;
    const item = list?.querySelector<HTMLElement>('[aria-current="location"]');
    if (expanded && list && item && (justOpened || (!list.matches(":hover") && !list.contains(document.activeElement)))) {
      revealItem(list, item);
    }
  }, [activeRoot, activeVisible, expanded]);

  useEffect(() => {
    if (!expanded) return;
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !navRef.current?.contains(event.target)) closeOutline();
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [expanded, closeOutline]);

  if (!editor || editor.isDestroyed || !headings.length) return null;
  const tabPosition = visible.some(({ heading }) => heading.position === focused) ? focused : activeVisible ?? tree.roots[0];
  const headingStyle = (entry: OutlineEntry<Heading>) => ({
    "--outline-depth": entry.level,
    "--outline-line-width": `${Math.max(8, 20 - entry.level * 4)}px`,
  } as CSSProperties);
  const openBranch = (path: number[]) => {
    // Keep the open card's hit area when a sibling replaces a longer branch.
    setBranch((previous) => ({ source: headings, path,
      rows: Math.max(previous?.source === headings ? previous.rows : 0, visibleOutlineEntries(tree, path).length),
    }));
  };
  const disclose = (entry: OutlineEntry<Heading>) => {
    openBranch(entry.children.length ? entry.path : entry.path.slice(0, -1));
  };
  const queueHover = (entry: OutlineEntry<Heading>, element: HTMLElement, x: number, y: number) => {
    // A branch collapse may move another row under a stationary pointer.
    // Only physical pointer movement starts a new hover intent.
    if (lastPointer.current?.x === x && lastPointer.current.y === y) return;
    lastPointer.current = { x, y };
    if (hoverTarget.current === entry.heading.position) return;
    cancelHover();
    hoverTarget.current = entry.heading.position;
    hoverTimer.current = setTimeout(() => {
      hoverTimer.current = null;
      if (element.isConnected && element.matches(":hover")) disclose(entry);
    }, HOVER_DELAY);
  };
  const focusEntry = (position: number) => requestAnimationFrame(() => {
    const button = listRef.current?.querySelector<HTMLButtonElement>(`[data-outline-position="${position}"]`);
    button?.focus({ preventScroll: true });
    if (listRef.current && button) revealItem(listRef.current, button);
  });
  const jump = (heading: Heading) => {
    cancelHover();
    const viewport = scrollContainerRef.current;
    if (!viewport || !heading.element.isConnected) return;
    const target = heading.element.getBoundingClientRect();
    const bounds = viewport.getBoundingClientRect();
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches
      || Boolean(viewport.closest(".profile-shell--reduce-motion"));
    viewport.scrollTo({
      top: Math.max(0, viewport.scrollTop + target.top - bounds.top - Math.min(64, viewport.clientHeight / 5)),
      behavior: reduceMotion ? "instant" : "smooth",
    });
  };
  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    keyboardMode.current = true;
    cancelHover();
    if (event.key === "Escape") {
      event.preventDefault();
      closeOutline();
      railRef.current?.focus({ preventScroll: true });
      return;
    }
    if (!["ArrowDown", "ArrowUp", "ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    setExpanded(true);
    const currentButton = document.activeElement as HTMLElement | null;
    const current = visible.findIndex(({ heading }) => String(heading.position) === currentButton?.dataset.outlinePosition);
    const entry = visible[current];
    if (event.key === "ArrowRight") {
      if (entry?.children.length) { disclose(entry); focusEntry(entry.children[0]); }
      else if (!entry) focusEntry(activeVisible ?? tree.roots[0]);
    } else if (event.key === "ArrowLeft") {
      if (entry && openPath.includes(entry.heading.position)) {
        openBranch(entry.path.slice(0, -1));
      } else if (entry?.parent !== null && entry?.parent !== undefined) focusEntry(entry.parent);
    } else {
      const next = event.key === "Home" ? 0 : event.key === "End" ? visible.length - 1
        : Math.max(0, Math.min(visible.length - 1, current + (event.key === "ArrowDown" ? 1 : -1)));
      focusEntry(visible[next].heading.position);
    }
  };

  return (
    <nav
      ref={navRef}
      aria-label="Document outline"
      className={styles.outline}
      data-expanded={expanded}
      style={{ "--outline-panel-width": `${Math.max(100, Math.min(304, width - 24))}px`,
        "--outline-root-count": roots.length, "--outline-visible-count": branch?.source === headings ? branch.rows : roots.length,
      } as CSSProperties}
      onPointerEnter={(event) => { if (event.pointerType !== "touch") setExpanded(true); }}
      onPointerMove={() => { keyboardMode.current = false; }}
      onPointerLeave={() => {
        cancelHover();
        if (!keyboardMode.current || !navRef.current?.contains(document.activeElement)) closeOutline();
      }}
      onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) closeOutline(); }}
      onKeyDown={handleKeyDown}
    >
      <button
        ref={railRef}
        type="button"
        className={styles.rail}
        aria-label="Show document outline"
        aria-expanded={expanded}
        aria-controls={panelId}
        tabIndex={expanded ? -1 : 0}
        onPointerDown={(event) => { if (event.pointerType === "mouse") event.preventDefault(); }}
        onClick={(event) => {
          setExpanded(true);
          if (event.detail === 0) focusEntry(activeRoot ?? tree.roots[0]);
        }}
      >
        {roots.map((entry) => (
          <span
            key={entry.heading.position}
            className={styles.marker}
            style={headingStyle(entry)}
            data-outline-marker="true"
            data-active={entry.heading.position === activeRoot}
            aria-hidden="true"
            onClick={(event) => { event.stopPropagation(); setExpanded(true); jump(entry.heading); }}
          ><span className={styles.line} /></span>
        ))}
      </button>
      <div id={panelId} className={styles.panel} hidden={!expanded}>
        <ol className={styles.list} ref={listRef}>
          {visible.map((entry) => (
            <li key={entry.heading.position}>
              <button
                type="button"
                className={styles.item}
                aria-label={`Jump to ${entry.heading.label}`}
                aria-current={entry.heading.position === activeVisible ? "location" : undefined}
                aria-expanded={entry.children.length ? openPath.includes(entry.heading.position) : undefined}
                data-outline-position={entry.heading.position}
                data-outline-level={entry.level + 1}
                tabIndex={entry.heading.position === tabPosition ? 0 : -1}
                style={headingStyle(entry)}
                title={entry.heading.label}
                onFocus={() => setFocused(entry.heading.position)}
                onPointerMove={(event) => { if (event.pointerType !== "touch") queueHover(entry, event.currentTarget, event.clientX, event.clientY); }}
                onPointerLeave={cancelHover}
                onPointerDown={(event) => {
                  if (event.pointerType === "mouse") event.preventDefault();
                  else disclose(entry);
                }}
                onClick={() => jump(entry.heading)}
              >
                <span className={styles.itemMarker} aria-hidden="true"><span className={styles.line} /></span>
                <span className={styles.label}>{entry.heading.label}</span>
                {entry.children.length ? <ChevronRight aria-hidden="true" className={styles.chevron} /> : null}
              </button>
            </li>
          ))}
        </ol>
      </div>
    </nav>
  );
}
