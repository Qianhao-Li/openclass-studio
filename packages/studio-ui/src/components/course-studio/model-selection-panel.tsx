import clsx from "clsx";
import { Check, Cpu, KeyRound, Landmark, UserRound } from "lucide-react";
import { useState } from "react";

import {
  MODEL_ACCESS_METHODS,
  PROVIDER_LABELS,
  modelAccessMethod,
  modelAccessMethodLabel,
  modelButtonLabel,
  modelOptionKey,
  modelSelectionKey,
  selectionForModelOption,
} from "./model-catalog";
import { PersonalApiProviderForm } from "./personal-api-provider-form";
import type {
  AIModelAccessMethod,
  AIModelOption,
  AIModelSelection,
} from "../../types";
import type { CodexConnectionControls } from "../../hooks/course-studio/use-model-catalog";

const ACCESS_METHOD_ICONS = {
  chatgpt_subscription: UserRound,
  personal_api: KeyRound,
  platform_credits: Landmark,
} satisfies Record<AIModelAccessMethod, typeof UserRound>;

type ModelSelectionPanelProps = {
  options: AIModelOption[];
  selectedModel: AIModelSelection;
  selectedOption: AIModelOption | null;
  onSelect: (selection: AIModelSelection) => void;
  codexConnectionControls?: CodexConnectionControls;
};

export function ModelSelectionPanel({
  options,
  selectedModel,
  selectedOption,
  onSelect,
  codexConnectionControls,
}: ModelSelectionPanelProps) {
  const selectedAccessMethod = modelAccessMethod(selectedOption ?? selectedModel);
  const [personalProvider, setPersonalProvider] = useState(
    selectedAccessMethod === "personal_api" ? selectedModel.provider : ""
  );
  const selectedKey = modelSelectionKey(selectedModel);
  const [previousSelectedKey, setPreviousSelectedKey] = useState(selectedKey);
  if (previousSelectedKey !== selectedKey) {
    setPreviousSelectedKey(selectedKey);
    if (selectedAccessMethod === "personal_api") setPersonalProvider(selectedModel.provider);
  }
  const [routeView, setRouteView] = useState({
    selected: selectedAccessMethod,
    browsing: selectedAccessMethod,
  });
  const visibleAccessMethod = routeView.selected === selectedAccessMethod
    ? routeView.browsing
    : selectedAccessMethod;
  if (routeView.selected !== selectedAccessMethod) {
    setRouteView({ selected: selectedAccessMethod, browsing: selectedAccessMethod });
    if (selectedAccessMethod === "personal_api") setPersonalProvider(selectedModel.provider);
  }
  const visibleOptions = options.filter(
    (option) => modelAccessMethod(option) === visibleAccessMethod &&
      (visibleAccessMethod !== "personal_api" || !personalProvider || option.provider === personalProvider)
  );

  function selectAccessMethod(accessMethod: AIModelAccessMethod) {
    setRouteView({ selected: selectedAccessMethod, browsing: accessMethod });
    const routeOptions = options.filter(
      (option) => modelAccessMethod(option) === accessMethod && option.enabled &&
        (accessMethod !== "personal_api" || !personalProvider || option.provider === personalProvider)
    );
    const nextOption =
      routeOptions.find((option) => option.model === selectedModel.model) ??
      routeOptions.find((option) => option.default) ??
      routeOptions[0];
    if (nextOption) {
      onSelect(selectionForModelOption(nextOption, selectedModel));
    }
  }

  return (
    <div className="space-y-4">
      {codexConnectionControls && (codexConnectionControls.connections.length > 0 || codexConnectionControls.error) ? <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <label className="block text-sm font-semibold text-gray-950">ChatGPT account for subscription models
          <select aria-label="ChatGPT model account" className="mt-2 w-full rounded-md border border-gray-200 bg-white p-2 text-sm" value={codexConnectionControls.selectedId ?? ""} disabled={codexConnectionControls.busy || !codexConnectionControls.connections.length} onChange={(event) => { if (event.target.value) void codexConnectionControls.select(event.target.value); }}>
            <option value="" disabled>Choose a connected account</option>
            {codexConnectionControls.connections.map((entry) => <option key={entry.id} value={entry.id}>{entry.display_name}</option>)}
          </select>
        </label>
        {codexConnectionControls.connections.length > 1 && !codexConnectionControls.selectedId ? <p className="mt-2 text-xs text-amber-700"><span>Choose an account before using ChatGPT subscription models.</span></p> : null}
        {codexConnectionControls.error ? <p key={codexConnectionControls.error} role="alert" className="mt-2 text-xs text-red-700"><span>{codexConnectionControls.error}</span></p> : null}
      </section> : null}
      <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className="text-[10px] font-bold uppercase tracking-wider text-gray-400">

              current model
            </p>
            <p className="mt-1 text-sm font-semibold text-gray-950">
              {modelButtonLabel(selectedOption, selectedModel)}
            </p>
            <p className="mt-1 text-xs text-gray-500">
              {modelAccessMethodLabel(selectedOption ?? selectedModel)}  · Share selection state with chat input box
            </p>
          </div>
          <Cpu className="h-5 w-5 shrink-0 text-gray-400" />
        </div>
      </section>

      <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <h3 className="text-sm font-semibold text-gray-950">Calling route</h3>
        <div className="mt-3 space-y-2">
          {MODEL_ACCESS_METHODS.map((method) => {
            const Icon = ACCESS_METHOD_ICONS[method.id];
            const routeEnabled = options.some(
              (option) =>
                modelAccessMethod(option) === method.id && option.enabled
            );
            const canConfigurePersonal = method.id === "personal_api";
            const enabled = routeEnabled || canConfigurePersonal;
            const active = visibleAccessMethod === method.id;
            return (
              <button
                key={method.id}
                type="button"
                disabled={!enabled}
                aria-pressed={active}
                onClick={() => selectAccessMethod(method.id)}
                className={clsx(
                  "w-full rounded-lg border p-3 text-left transition",
                  active
                    ? "border-gray-950 bg-gray-950 text-white"
                    : "border-gray-200 bg-white hover:border-gray-400",
                  !enabled && "cursor-not-allowed opacity-50"
                )}
              >
                <span className="flex items-center gap-2">
                  <Icon className="h-4 w-4 shrink-0" />
                  <span className="font-semibold">{method.label}</span>
                  {active ? <Check className="ml-auto h-4 w-4" /> : null}
                </span>
                <span
                  className={clsx(
                    "mt-1 block text-xs leading-5",
                    active ? "text-gray-300" : "text-gray-500"
                  )}
                >
                  {method.description}
                  {!routeEnabled && !canConfigurePersonal ? "Not connected yet." : ""}
                </span>
              </button>
            );
          })}
        </div>

        <PersonalApiProviderForm provider={personalProvider} onProviderChange={(provider) => {
          setPersonalProvider(provider);
          setRouteView({ selected: selectedAccessMethod, browsing: "personal_api" });
        }} />
      </section>

      <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <h3 className="text-sm font-semibold text-gray-950">Available models</h3>
        {visibleAccessMethod === "personal_api" && !visibleOptions.some((option) => option.enabled) ? (
          <p className="mt-2 text-xs leading-5 text-gray-500">
            Save your provider API key above to enable its models, then select a model.
          </p>
        ) : null}
        <div className="mt-3 space-y-2">
          {visibleOptions.map((option) => {
            const active =
              modelOptionKey(option) === modelSelectionKey(selectedModel);
            return (
              <button
                key={modelOptionKey(option)}
                type="button"
                disabled={!option.enabled}
                onClick={() =>
                  onSelect(selectionForModelOption(option, selectedModel))
                }
                className={clsx(
                  "w-full rounded-lg border p-3 text-left transition",
                  active
                    ? "border-gray-950 bg-gray-950 text-white"
                    : "border-gray-200 bg-white hover:border-gray-400",
                  !option.enabled && "cursor-not-allowed opacity-50",
                )}
              >
                <span className="flex items-center justify-between gap-3">
                  <span className="font-semibold">{option.label}</span>
                  {active ? <Check className="h-4 w-4" /> : null}
                </span>
                <span
                  className={clsx(
                    "mt-1 block text-xs",
                    active ? "text-gray-300" : "text-gray-500",
                  )}
                >
                  {PROVIDER_LABELS[option.provider] ?? option.provider} · {option.enabled ? "Available" : "Not configured yet"}
                </span>
                {visibleAccessMethod === "platform_credits" ? (
                  <span
                    className={clsx(
                      "mt-1.5 block text-xs font-medium",
                      active ? "text-emerald-300" : "text-emerald-700",
                    )}
                  >

                    Every 1 million input tokens ·{" "}
                    {option.input_price_credits_per_million == null
                      ? "Price is not available yet"
                      : `${option.input_price_credits_per_million.toLocaleString("en-US")} platform credits`}
                  </span>
                ) : null}
              </button>
            );
          })}
        </div>
      </section>
    </div>
  );
}
