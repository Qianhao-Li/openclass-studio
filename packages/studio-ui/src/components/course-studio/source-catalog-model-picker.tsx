"use client";

import { useState } from "react";

import { CodexModelSettingsPicker } from "./codex-model-settings-picker";
import {
  findModelOption,
  selectionForModelOption,
} from "./model-catalog";
import type { AIModelOption, AIModelSelection } from "../../types";

export function SourceCatalogModelPicker({
  options,
  selection,
  defaultSelection,
  disabled,
  onChange,
}: {
  options: AIModelOption[];
  selection: AIModelSelection;
  defaultSelection: AIModelSelection;
  disabled: boolean;
  onChange: (selection: AIModelSelection) => void;
}) {
  const [open, setOpen] = useState(false);
  const selectedOption = findModelOption(options, selection);
  const enabledOption = selectedOption?.enabled
    ? selectedOption
    : options.find((option) => option.enabled) ?? null;
  const displayOption = enabledOption ?? selectedOption ?? options[0] ?? null;
  const normalizedSelection = displayOption
    ? selectionForModelOption(displayOption, selection)
    : selection;

  return (
    <div className="mt-3" data-testid="source-catalog-model-picker">
      <p className="text-[11px] font-bold uppercase tracking-widest text-gray-500">

        Directory extraction model
      </p>
      <div className="mt-2">
        <CodexModelSettingsPicker
          open={open}
          onOpenChange={setOpen}
          selectedModel={normalizedSelection}
          selectedOption={displayOption}
          defaultSelection={defaultSelection}
          options={options}
          onChange={onChange}
          disabled={disabled || options.length === 0}
          contextLabel="Catalog Extraction Model Settings"
          testIdPrefix="source-catalog-model"
          preferredPlacement="below"
          preferredSubmenuSide="left"
        />
        {!enabledOption ? (
          <p className="mt-1.5 text-[11px] leading-5 text-amber-700">

            There is no configured directory to extract the model; you can expand it to view the model status.
          </p>
        ) : null}
      </div>
    </div>
  );
}
