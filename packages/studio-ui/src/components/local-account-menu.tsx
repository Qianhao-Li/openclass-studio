"use client";

import { useEffect, useRef, useState } from "react";
import { FolderClosed, KeyRound, LogIn, UserRound } from "lucide-react";

type LocalAccountMenuProps = {
  connected: boolean;
  onCourses: () => void;
  onModels: () => void;
  onPlatform: () => void;
};

export function LocalAccountMenu({ connected, onCourses, onModels, onPlatform }: LocalAccountMenuProps) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    function onPointerDown(event: PointerEvent) {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setOpen(false);
        trigger.current?.focus();
      }
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  function choose(action: () => void) {
    setOpen(false);
    action();
  }

  const itemClass = "flex w-full items-center gap-3 px-4 py-3 text-left text-sm text-stone-700 outline-none hover:bg-stone-50 focus:bg-stone-100";
  return <div ref={root} className="relative" data-account-menu-root>
    <button
      ref={trigger}
      type="button"
      aria-label="OpenClass User Avatar"
      aria-haspopup="menu"
      aria-expanded={open}
      title={connected ? "OpenClass account" : "Account and sign in"}
      onClick={() => setOpen(value => !value)}
      className="inline-flex h-11 w-11 items-center justify-center overflow-hidden rounded-full border-2 border-white bg-stone-200 text-stone-600 shadow-[0_10px_24px_rgba(15,23,42,0.08)] transition hover:scale-[1.03] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-stone-500"
    ><UserRound className="h-6 w-6" aria-hidden="true" /></button>
    {open ? <div
      ref={menu}
      role="menu"
      aria-label="Account menu"
      className="absolute right-0 z-[120] mt-2 w-72 max-w-[calc(100vw-1.5rem)] overflow-hidden rounded-lg border border-stone-200 bg-white text-left shadow-[0_24px_60px_rgba(15,23,42,0.16)]"
      onKeyDown={event => {
        const items = Array.from(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') || []);
        const index = items.indexOf(document.activeElement as HTMLButtonElement);
        if (event.key === "ArrowDown" || event.key === "ArrowUp") {
          event.preventDefault();
          items[(index + (event.key === "ArrowDown" ? 1 : items.length - 1)) % items.length]?.focus();
        }
      }}
    >
      <div className="border-b border-stone-200 px-4 py-3">
        <p className="text-sm font-semibold text-stone-950">On this device</p>
        <p className="mt-1 text-xs text-stone-500">{connected ? "OpenClass account connected" : "Local editing works without signing in."}</p>
      </div>
      <button type="button" role="menuitem" className={itemClass} onClick={() => choose(onPlatform)}>
        <LogIn className="h-4 w-4" aria-hidden="true" />{connected ? "OpenClass account" : "Sign in to OpenClass"}
      </button>
      <button type="button" role="menuitem" className={itemClass} onClick={() => choose(onModels)}>
        <KeyRound className="h-4 w-4" aria-hidden="true" />Model settings
      </button>
      <button type="button" role="menuitem" className={itemClass} onClick={() => choose(onCourses)}>
        <FolderClosed className="h-4 w-4" aria-hidden="true" />Courses on this device
      </button>
    </div> : null}
  </div>;
}
