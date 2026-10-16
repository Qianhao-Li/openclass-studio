import clsx from "clsx";
import { Check, ChevronDown, ChevronRight, RotateCcw, Zap } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  modelAccessMethodLabel,
  modelOptionKey,
  modelSelectionKey,
  selectionForModelOption,
} from "./model-catalog";
import type {
  AIModelOption,
  AIModelSelection,
  AIReasoningEffortOption,
  AIServiceTierOption,
} from "../../types";

type SettingsMenu = "model" | "reasoning" | "speed" | null;
type MenuPlacement = "above" | "below";
type SubmenuSide = "left" | "right";
type MenuPosition = { left: number; top: number };

const MENU_GAP = 8;
const VIEWPORT_PADDING = 8;

function clamp(value: number, minimum: number, maximum: number) {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
}

const REASONING_EFFORT_LABELS: Record<string, string> = {
  none: "none",
  minimal: "extremely low",
  low: "Mild",
  medium: "middle",
  high: "high",
  xhigh: "extremely high",
  max: "Highest",
  ultra: "extremely high",
};

function reasoningEffortLabel(effort: string | null | undefined) {
  if (!effort) {
    return "default";
  }
  return REASONING_EFFORT_LABELS[effort] ?? effort;
}

function reasoningEffortDescription(option: AIReasoningEffortOption) {
  return option.reasoning_effort === "ultra" ? "Use more credit" : "";
}

function shortModelLabel(option: AIModelOption | null, selection: AIModelSelection) {
  const source = option?.label || selection.model;
  return source
    .replace(/^OpenAI Codex\s+/i, "")
    .replace(/^GPT-/i, "")
    .replaceAll("-", " ");
}

function serviceTierLabel(option: AIServiceTierOption) {
  return option.id === "priority" ? "fast" : option.name || option.id;
}

function serviceTierDescription(option: AIServiceTierOption) {
  return option.id === "priority" ? "1.5 times faster, more usage" : option.description;
}

function SettingsRow({
  label,
  value,
  active,
  testId,
  onClick,
}: {
  label: string;
  value: string;
  active: boolean;
  testId?: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-expanded={active}
      onClick={onClick}
      className={clsx(
        "flex h-10 w-full items-center gap-3 rounded-lg px-2.5 text-left text-sm transition-colors",
        active ? "bg-gray-100" : "hover:bg-gray-50"
      )}
    >
      <span className="font-semibold text-gray-950">{label}</span>
      <span className="ml-auto max-w-28 truncate text-gray-500">{value}</span>
      <ChevronRight className="h-4 w-4 shrink-0 text-gray-400" />
    </button>
  );
}

function OptionButton({
  label,
  description,
  selected,
  ariaLabel,
  disabled = false,
  onClick,
}: {
  label: string;
  description?: string;
  selected: boolean;
  ariaLabel: string;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      aria-pressed={selected}
      disabled={disabled}
      onClick={onClick}
      className="flex min-h-10 w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm text-gray-900 transition-colors hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-transparent"
    >
      <span className="min-w-0 flex-1">
        <span className="block truncate font-medium">{label}</span>
        {description ? <span className="mt-0.5 block text-xs leading-4 text-gray-400">{description}</span> : null}
      </span>
      {selected ? <Check className="h-4 w-4 shrink-0 text-gray-900" /> : null}
    </button>
  );
}

export function CodexModelSettingsPicker({
  open,
  onOpenChange,
  selectedModel,
  selectedOption,
  defaultSelection,
  options,
  onChange,
  disabled = false,
  contextLabel = "Model settings",
  testIdPrefix = "codex-model",
  preferredPlacement = "above",
  preferredSubmenuSide = "right",
  allowSpeedSelection = true,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  selectedModel: AIModelSelection;
  selectedOption: AIModelOption | null;
  defaultSelection: AIModelSelection;
  options: AIModelOption[];
  onChange: (selection: AIModelSelection) => void;
  disabled?: boolean;
  contextLabel?: string;
  testIdPrefix?: string;
  preferredPlacement?: MenuPlacement;
  preferredSubmenuSide?: SubmenuSide;
  allowSpeedSelection?: boolean;
}) {
  const [activeMenu, setActiveMenu] = useState<SettingsMenu>(null);
  const [menuPosition, setMenuPosition] = useState<MenuPosition | null>(null);
  const [submenuPosition, setSubmenuPosition] = useState<MenuPosition | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const submenuRef = useRef<HTMLDivElement | null>(null);
  const normalizedSelection = selectedOption
    ? selectionForModelOption(selectedOption, selectedModel)
    : selectedModel;
  const reasoningOptions = selectedOption?.supported_reasoning_efforts ?? [];
  const serviceTiers = allowSpeedSelection ? selectedOption?.service_tiers ?? [] : [];
  const selectedServiceTier =
    serviceTiers.find((option) => option.id === normalizedSelection.service_tier) ?? null;
  const hasSelectableReasoning = reasoningOptions.length > 1;
  const hasSelectableSpeed = serviceTiers.length > 0;
  const modelLabel = shortModelLabel(selectedOption, normalizedSelection);
  const effortLabel = reasoningEffortLabel(normalizedSelection.reasoning_effort);
  const speedLabel = selectedServiceTier ? serviceTierLabel(selectedServiceTier) : "standard";

  function applySelection(selection: AIModelSelection) {
    setActiveMenu(null);
    onChange(selection);
    onOpenChange(true);
  }

  function togglePicker() {
    if (disabled) {
      return;
    }
    setActiveMenu(null);
    onOpenChange(!open);
  }

  function resetDefaults() {
    const defaultOption =
      options.find((option) => modelOptionKey(option) === modelSelectionKey(defaultSelection)) ?? null;
    applySelection(defaultOption ? selectionForModelOption(defaultOption, defaultSelection) : defaultSelection);
  }

  useEffect(() => {
    if (!open) {
      return;
    }

    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (
        triggerRef.current?.contains(target) ||
        menuRef.current?.contains(target) ||
        submenuRef.current?.contains(target)
      ) {
        return;
      }
      setActiveMenu(null);
      onOpenChange(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") {
        return;
      }
      setActiveMenu(null);
      onOpenChange(false);
      triggerRef.current?.focus();
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [onOpenChange, open]);

  useEffect(() => {
    if (disabled && open) {
      onOpenChange(false);
    }
  }, [disabled, onOpenChange, open]);

  useLayoutEffect(() => {
    if (!open) {
      return;
    }

    let animationFrame = 0;
    function updatePositions() {
      const trigger = triggerRef.current;
      const menu = menuRef.current;
      if (!trigger || !menu) {
        return;
      }

      const triggerRect = trigger.getBoundingClientRect();
      const menuRect = menu.getBoundingClientRect();
      const spaceAbove = triggerRect.top - VIEWPORT_PADDING;
      const spaceBelow = window.innerHeight - triggerRect.bottom - VIEWPORT_PADDING;
      const fitsAbove = spaceAbove >= menuRect.height + MENU_GAP;
      const fitsBelow = spaceBelow >= menuRect.height + MENU_GAP;
      const placeBelow =
        preferredPlacement === "below"
          ? fitsBelow || (!fitsAbove && spaceBelow >= spaceAbove)
          : !fitsAbove && (fitsBelow || spaceBelow > spaceAbove);
      const menuLeft = clamp(
        triggerRect.left,
        VIEWPORT_PADDING,
        window.innerWidth - menuRect.width - VIEWPORT_PADDING
      );
      const menuTop = clamp(
        placeBelow
          ? triggerRect.bottom + MENU_GAP
          : triggerRect.top - menuRect.height - MENU_GAP,
        VIEWPORT_PADDING,
        window.innerHeight - menuRect.height - VIEWPORT_PADDING
      );
      setMenuPosition({ left: menuLeft, top: menuTop });

      const submenu = submenuRef.current;
      if (!submenu) {
        setSubmenuPosition(null);
        return;
      }
      const submenuRect = submenu.getBoundingClientRect();
      const leftCandidate = menuLeft - submenuRect.width - MENU_GAP;
      const rightCandidate = menuLeft + menuRect.width + MENU_GAP;
      const fitsLeft = leftCandidate >= VIEWPORT_PADDING;
      const fitsRight = rightCandidate + submenuRect.width <= window.innerWidth - VIEWPORT_PADDING;
      const placeLeft =
        preferredSubmenuSide === "left"
          ? fitsLeft || !fitsRight
          : !fitsRight && fitsLeft;
      const submenuLeft = clamp(
        placeLeft ? leftCandidate : rightCandidate,
        VIEWPORT_PADDING,
        window.innerWidth - submenuRect.width - VIEWPORT_PADDING
      );
      const submenuTop = clamp(
        placeBelow
          ? menuTop
          : menuTop + menuRect.height - submenuRect.height,
        VIEWPORT_PADDING,
        window.innerHeight - submenuRect.height - VIEWPORT_PADDING
      );
      setSubmenuPosition({ left: submenuLeft, top: submenuTop });
    }

    function scheduleUpdate() {
      window.cancelAnimationFrame(animationFrame);
      animationFrame = window.requestAnimationFrame(updatePositions);
    }

    updatePositions();
    window.addEventListener("resize", scheduleUpdate);
    window.addEventListener("scroll", scheduleUpdate, true);
    return () => {
      window.cancelAnimationFrame(animationFrame);
      window.removeEventListener("resize", scheduleUpdate);
      window.removeEventListener("scroll", scheduleUpdate, true);
    };
  }, [activeMenu, open, preferredPlacement, preferredSubmenuSide]);

  const menuLayer =
    open && typeof document !== "undefined"
      ? createPortal(
          <>
            <div
              ref={menuRef}
              data-testid={`${testIdPrefix}-settings-menu`}
              style={{
                left: menuPosition?.left ?? 0,
                top: menuPosition?.top ?? 0,
                visibility: menuPosition ? "visible" : "hidden",
              }}
              className="fixed z-[100] w-56 rounded-xl border border-gray-200 bg-white p-1.5 shadow-[0_18px_50px_rgba(0,0,0,0.16)]"
            >
              <SettingsRow
                label="Model"
                value={modelLabel}
                active={activeMenu === "model"}
                testId={`${testIdPrefix}-model-row`}
                onClick={() => setActiveMenu((current) => (current === "model" ? null : "model"))}
              />
              {hasSelectableReasoning ? (
                <SettingsRow
                  label="Reasoning strength"
                  value={effortLabel}
                  active={activeMenu === "reasoning"}
                  testId={`${testIdPrefix}-reasoning-row`}
                  onClick={() => setActiveMenu((current) => (current === "reasoning" ? null : "reasoning"))}
                />
              ) : null}
              {hasSelectableSpeed ? (
                <SettingsRow
                  label="speed"
                  value={speedLabel}
                  active={activeMenu === "speed"}
                  testId={`${testIdPrefix}-speed-row`}
                  onClick={() => setActiveMenu((current) => (current === "speed" ? null : "speed"))}
                />
              ) : null}
              <div className="my-1 h-px bg-gray-100" />
              <button
                type="button"
                data-testid={`${testIdPrefix}-reset-button`}
                onClick={resetDefaults}
                className="flex h-10 w-full items-center rounded-lg px-2.5 text-left text-sm text-gray-500 transition-colors hover:bg-gray-50 hover:text-gray-900"
              >
                <span>Reset to default settings</span>
                <RotateCcw className="ml-auto h-4 w-4" />
              </button>
            </div>

            {activeMenu ? (
              <div
                ref={submenuRef}
                data-testid={`${testIdPrefix}-${activeMenu}-menu`}
                style={{
                  left: submenuPosition?.left ?? 0,
                  top: submenuPosition?.top ?? 0,
                  visibility: submenuPosition ? "visible" : "hidden",
                }}
                className="fixed z-[110] max-h-[420px] w-72 overflow-y-auto rounded-xl border border-gray-200 bg-white p-1.5 shadow-[0_18px_50px_rgba(0,0,0,0.16)]"
              >
                <p className="px-2.5 pb-1 pt-1.5 text-sm text-gray-400">
                  {activeMenu === "model" ? "Model" : activeMenu === "reasoning" ? "Reasoning strength" : "speed"}
                </p>

                {activeMenu === "model"
                  ? options.map((option) => (
                      <OptionButton
                        key={modelOptionKey(option)}
                        label={shortModelLabel(option, normalizedSelection)}
                        description={modelAccessMethodLabel(option)}
                        selected={modelOptionKey(option) === modelSelectionKey(normalizedSelection)}
                        ariaLabel={`Select model ${shortModelLabel(option, normalizedSelection)}`}
                        disabled={!option.enabled}
                        onClick={() => applySelection(selectionForModelOption(option, normalizedSelection))}
                      />
                    ))
                  : null}

                {activeMenu === "reasoning"
                  ? reasoningOptions.map((option) => (
                      <OptionButton
                        key={option.reasoning_effort}
                        label={reasoningEffortLabel(option.reasoning_effort)}
                        description={reasoningEffortDescription(option)}
                        selected={option.reasoning_effort === normalizedSelection.reasoning_effort}
                        ariaLabel={`Reasoning effort ${reasoningEffortLabel(option.reasoning_effort)}${option.reasoning_effort === "ultra" ? " Ultra" : ""}`}
                        onClick={() =>
                          applySelection({
                            ...normalizedSelection,
                            reasoning_effort: option.reasoning_effort,
                          })
                        }
                      />
                    ))
                  : null}

                {activeMenu === "speed" ? (
                  <>
                    <OptionButton
                      label="standard"
                      description="Default speed"
                      selected={!normalizedSelection.service_tier}
                      ariaLabel="speed standard"
                      onClick={() => applySelection({ ...normalizedSelection, service_tier: null })}
                    />
                    {serviceTiers.map((option) => (
                      <OptionButton
                        key={option.id}
                        label={serviceTierLabel(option)}
                        description={serviceTierDescription(option)}
                        selected={option.id === normalizedSelection.service_tier}
                        ariaLabel={`Speed ${serviceTierLabel(option)}`}
                        onClick={() => applySelection({ ...normalizedSelection, service_tier: option.id })}
                      />
                    ))}
                  </>
                ) : null}
              </div>
            ) : null}
          </>,
          document.body
        )
      : null;

  return (
    <div className="relative min-w-0">
      <button
        ref={triggerRef}
        type="button"
        data-testid={`${testIdPrefix}-settings-button`}
        aria-expanded={open}
        aria-label={`${contextLabel}, current ${modelLabel}, reasoning effort ${effortLabel}, speed ${speedLabel}`}
        disabled={disabled}
        onClick={togglePicker}
        className="flex h-10 w-full items-center justify-center gap-1.5 rounded-full bg-gray-100 px-3 text-sm text-gray-900 transition-colors hover:bg-gray-200 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-gray-100"
      >
        <Zap className="h-3.5 w-3.5 shrink-0 fill-current" />
        <span className="truncate font-medium">{modelLabel}</span>
        <span className="shrink-0 font-medium text-violet-600">{effortLabel}</span>
        <ChevronDown className={clsx("ml-auto h-4 w-4 shrink-0 text-gray-400 transition-transform", open && "rotate-180")} />
      </button>
      {menuLayer}
    </div>
  );
}
