import type {
  AIModelAccessMethod,
  AIModelCatalog,
  AIModelOption,
  AIModelSelection,
} from "../../types";

export type GoogleRealtimeAudioMessage = {
  setupComplete?: Record<string, unknown>;
  error?: {
    code?: number;
    message?: string;
    status?: string;
  };
  serverContent?: {
    modelTurn?: {
      parts?: Array<{
        inlineData?: {
          mimeType?: string;
          data?: string;
        };
        text?: string;
      }>;
    };
    inputTranscription?: {
      text?: string;
    };
    outputTranscription?: {
      text?: string;
    };
    turnComplete?: boolean;
    interrupted?: boolean;
  };
};

const CODEX_TEXT_MODELS = [
  ["gpt-6.1-sol", "GPT 6.1 Sol"],
  ["gpt-6-astra", "GPT 6 Astra"],
  ["gpt-6-sol", "GPT 6 Sol"],
  ["gpt-6-luna", "GPT 6 Luna"],
  ["gpt-5.6-sol", "GPT 5.6 Sol"],
  ["gpt-5.6-terra", "GPT 5.6 Terra"],
  ["gpt-5.6-luna", "GPT 5.6 Luna"],
] as const;
const CODEX_TEXT_MODEL_IDS = new Set<string>(CODEX_TEXT_MODELS.map(([model]) => model));
const DEFAULT_CODEX_TEXT_MODEL = "gpt-5.6-luna";

export const FALLBACK_MODEL_CATALOG: AIModelCatalog = {
  text: CODEX_TEXT_MODELS.map(([model, label]) => ({
    provider: "openai_codex",
    model,
    access_method: "chatgpt_subscription",
    label,
    capability: "text",
    enabled: false,
    configured: false,
    default: model === DEFAULT_CODEX_TEXT_MODEL,
  })),
  realtime: [
    {
      provider: "openai",
      model: "gpt-realtime-2.1",
      access_method: "platform_credits",
      label: "OpenAI GPT Realtime 2.1",
      capability: "realtime",
      enabled: false,
      configured: false,
      default: true,
      transport: "openai_webrtc",
    },
  ],
  defaults: {
    text: {
      agent_backend: "pi",
      provider: "openai_codex",
      model: DEFAULT_CODEX_TEXT_MODEL,
      access_method: "chatgpt_subscription",
    },
    realtime: {
      provider: "openai",
      model: "gpt-realtime-2.1",
      access_method: "platform_credits",
    },
  },
};

export const MODEL_CREDENTIALS_CHANGED_EVENT = "openclass:model-credentials-changed";

export const MODEL_ACCESS_METHODS: ReadonlyArray<{
  id: AIModelAccessMethod;
  label: string;
  shortLabel: string;
  description: string;
}> = [
  {
    id: "chatgpt_subscription",
    label: "ChatGPT subscription quota",
    shortLabel: "ChatGPT Subscription",
    description: "Call the Codex subscription model through the connected ChatGPT account.",
  },
  {
    id: "personal_api",
    label: "Own model API",
    shortLabel: "Own API",
    description: "Use your API Key and be billed directly by the model service provider.",
  },
  {
    id: "platform_credits",
    label: "OpenClass Platform API",
    shortLabel: "Platform API",
    description: "Model service configured using OpenClass.",
  },
];

const DEFAULT_ACCESS_METHOD_BY_PROVIDER: Partial<
  Record<AIModelSelection["provider"], AIModelAccessMethod>
> = {
  openai_codex: "chatgpt_subscription",
  openai: "platform_credits",
  deepseek: "platform_credits",
  google: "platform_credits",
};

export function modelAccessMethod(
  selection: Pick<AIModelSelection, "provider" | "access_method">
): AIModelAccessMethod {
  return (
    selection.access_method ??
    DEFAULT_ACCESS_METHOD_BY_PROVIDER[selection.provider] ??
    "personal_api"
  );
}

export function modelAccessMethodLabel(
  selection: AIModelSelection | AIModelOption
): string {
  const method = modelAccessMethod(selection);
  return MODEL_ACCESS_METHODS.find((item) => item.id === method)?.label ?? method;
}

export const PROVIDER_LABELS: Record<AIModelSelection["provider"], string> = {
  openai: "OpenAI",
  openai_codex: "OpenAI Codex",
  anthropic: "Anthropic",
  google: "Google",
  deepseek: "DeepSeek",
  kimi: "Kimi",
  minimax: "MiniMax",
  openai_compatible: "OpenAI compatible",
  anthropic_compatible: "Anthropic compatible",
};

export const TEXT_MODEL_STORAGE_KEY = "blackboard-ai:selected-text-model";
export const REALTIME_MODEL_STORAGE_KEY = "blackboard-ai:selected-realtime-model";

const DISABLED_TEXT_MODEL_PROVIDERS = new Set<AIModelSelection["provider"]>();
const DISABLED_REALTIME_MODEL_PROVIDERS = new Set<AIModelSelection["provider"]>();

export function modelSelectionKey(selection: AIModelSelection): string {
  return `${modelAccessMethod(selection)}:${selection.provider}:${selection.model}${selection.connection_id ? `:${selection.connection_id}` : ""}`;
}

export function modelOptionKey(option: AIModelOption): string {
  return `${modelAccessMethod(option)}:${option.provider}:${option.model}${option.connection_id ? `:${option.connection_id}` : ""}`;
}

export function findModelOption(options: AIModelOption[], selection: AIModelSelection | null): AIModelOption | null {
  if (!selection) {
    return null;
  }
  return options.find((option) => modelOptionKey(option) === modelSelectionKey(selection)) ?? null;
}

function findEnabledModelOption(options: AIModelOption[], selection: AIModelSelection | null): AIModelOption | null {
  const option = findModelOption(options, selection);
  return option?.enabled ? option : null;
}

export function normalizeCourseStudioModelCatalog(catalog: AIModelCatalog): AIModelCatalog {
  return {
    ...catalog,
    text: catalog.text.filter((option) =>
      option.provider !== "openai_codex" || CODEX_TEXT_MODEL_IDS.has(option.model)
    ).map((option) =>
      DISABLED_TEXT_MODEL_PROVIDERS.has(option.provider)
        ? { ...option, enabled: false, configured: false, default: false }
        : option
    ),
    realtime: catalog.realtime.map((option) =>
      DISABLED_REALTIME_MODEL_PROVIDERS.has(option.provider)
        ? { ...option, enabled: false, configured: false, default: false }
        : option
    ),
  };
}

export function catalogForCodexConnection(base: AIModelCatalog, scoped: AIModelCatalog, connectionId: string): AIModelCatalog {
  const subscription = scoped.text.filter((option) => option.provider === "openai_codex" && modelAccessMethod(option) === "chatgpt_subscription")
    .map((option) => ({ ...option, connection_id: connectionId }));
  return normalizeCourseStudioModelCatalog({
    ...base,
    text: [...base.text.filter((option) => !(option.provider === "openai_codex" && modelAccessMethod(option) === "chatgpt_subscription")), ...subscription],
    defaults: { ...base.defaults, text: scoped.defaults.text.provider === "openai_codex" && modelAccessMethod(scoped.defaults.text) === "chatgpt_subscription"
      ? { ...scoped.defaults.text, connection_id: connectionId } : base.defaults.text },
  });
}

export function catalogAwaitingCodexChoice(base: AIModelCatalog): AIModelCatalog {
  return { ...base, text: base.text.map((option) => option.provider === "openai_codex" && modelAccessMethod(option) === "chatgpt_subscription"
    ? { ...option, enabled: false, configured: false, connection_id: null } : option) };
}

export function modelButtonLabel(option: AIModelOption | null, fallback: AIModelSelection | null): string {
  if (option) {
    return option.label;
  }
  if (!fallback) {
    return "Not selected";
  }
  return `${PROVIDER_LABELS[fallback.provider] ?? fallback.provider} ${fallback.model}`;
}

export function optionToSelection(option: AIModelOption): AIModelSelection {
  return selectionForModelOption(option, null);
}

export function selectionForModelOption(
  option: AIModelOption,
  current: AIModelSelection | null
): AIModelSelection {
  const reasoningOptions = option.supported_reasoning_efforts;
  const knownReasoningOptions = reasoningOptions ?? [];
  const supportedEfforts = new Set(knownReasoningOptions.map((item) => item.reasoning_effort));
  const currentEffort = current?.reasoning_effort?.trim() || null;
  const defaultEffort = option.default_reasoning_effort?.trim() || null;
  const reasoningEffort = reasoningOptions === undefined
    ? currentEffort ?? defaultEffort
    : supportedEfforts.size
      ? (currentEffort && supportedEfforts.has(currentEffort) ? currentEffort : null) ??
        (defaultEffort && supportedEfforts.has(defaultEffort) ? defaultEffort : null) ??
        knownReasoningOptions[0]?.reasoning_effort ??
        null
      : defaultEffort;

  const serviceTiers = option.service_tiers;
  const supportedServiceTiers = new Set((serviceTiers ?? []).map((item) => item.id));
  const currentHasServiceTier = Boolean(current && Object.hasOwn(current, "service_tier"));
  const currentServiceTier = current?.service_tier?.trim() || null;
  const defaultServiceTier = option.default_service_tier?.trim() || null;
  const serviceTier =
    serviceTiers === undefined
      ? currentHasServiceTier
        ? currentServiceTier
        : defaultServiceTier
      : currentHasServiceTier && currentServiceTier === null
        ? null
        : (currentServiceTier && supportedServiceTiers.has(currentServiceTier) ? currentServiceTier : null) ??
          (defaultServiceTier && supportedServiceTiers.has(defaultServiceTier) ? defaultServiceTier : null);

  return {
    agent_backend: "pi",
    provider: option.provider,
    model: option.model,
    access_method: option.access_method,
    connection_id: option.connection_id ?? null,
    reasoning_effort: reasoningEffort,
    service_tier: serviceTier,
  };
}

function isModelSelection(value: unknown): value is AIModelSelection {
  if (!value || typeof value !== "object") {
    return false;
  }
  const candidate = value as Partial<AIModelSelection>;
  return (
    typeof candidate.provider === "string" &&
    /^[a-z][a-z0-9_-]{0,63}$/.test(candidate.provider) &&
    typeof candidate.model === "string" &&
    candidate.model.trim().length > 0 &&
    (candidate.access_method == null ||
      MODEL_ACCESS_METHODS.some((item) => item.id === candidate.access_method)) &&
    (candidate.connection_id == null || typeof candidate.connection_id === "string") &&
    (candidate.reasoning_effort == null || typeof candidate.reasoning_effort === "string") &&
    (candidate.service_tier == null || typeof candidate.service_tier === "string")
  );
}

export function readStoredModelSelection(key: string): AIModelSelection | null {
  if (typeof window === "undefined") {
    return null;
  }
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) {
      return null;
    }
    const parsed = JSON.parse(raw) as unknown;
    return isModelSelection(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

export async function websocketMessageText(data: MessageEvent["data"]): Promise<string> {
  if (typeof data === "string") {
    return data;
  }
  if (data instanceof Blob) {
    return data.text();
  }
  if (data instanceof ArrayBuffer) {
    return new TextDecoder().decode(data);
  }
  if (ArrayBuffer.isView(data)) {
    return new TextDecoder().decode(data);
  }
  return String(data);
}

export function googleRealtimeErrorMessage(error: GoogleRealtimeAudioMessage["error"]): string {
  const rawMessage = error?.message?.trim() ?? "";
  const status = error?.status?.trim() ?? "";
  const lowerMessage = rawMessage.toLowerCase();
  const lowerStatus = status.toLowerCase();

  if (error?.code === 401 || lowerStatus.includes("unauthenticated")) {
    return "Google Gemini Live authentication failed. Please check whether the unified model API Key is correct.";
  }
  if (error?.code === 403 || lowerStatus.includes("permission") || lowerMessage.includes("permission denied")) {
    return "Google Gemini Live permission denied. Please check whether the Gemini API is enabled on the Google API Key and confirm that the key can use the Live API.";
  }
  if (error?.code === 429 || lowerStatus.includes("quota") || lowerMessage.includes("quota")) {
    return "The Google Gemini Live quota is insufficient or the requests are too frequent. Please try again later or check the Google API quota.";
  }
  if (rawMessage) {
    return `Google Gemini Live connection failed: ${rawMessage}`;
  }
  return "Google Gemini Live connection failed.";
}

export function realtimeConnectionErrorMessage(error: unknown, selection: AIModelSelection): string {
  const errorName = typeof error === "object" && error && "name" in error ? String(error.name) : "";
  const rawMessage = error instanceof Error ? error.message.trim() : "";
  const lowerMessage = rawMessage.toLowerCase();

  if (
    errorName === "NotAllowedError" ||
    errorName === "SecurityError" ||
    lowerMessage === "permission denied" ||
    lowerMessage.includes("permission dismissed")
  ) {
    return "Microphone permission denied. Please allow this website to use the microphone in the browser address bar; if it is opened through the local startup page, please reopen the startup page or click \"Open the front end directly\"; if it is not localhost, please open the page through HTTPS.";
  }
  if (errorName === "NotFoundError" || lowerMessage.includes("requested device not found")) {
    return "No available microphone found. Please connect or enable the microphone and try again.";
  }
  if (errorName === "NotReadableError" || lowerMessage.includes("could not start audio source")) {
    return "The microphone is temporarily unavailable and may be occupied by another application. Please close the application using the microphone and try again.";
  }
  if (rawMessage) {
    return rawMessage;
  }
  return `Could not connect to ${PROVIDER_LABELS[selection.provider]} realtime voice`;
}

export function persistModelSelection(key: string, selection: AIModelSelection) {
  if (typeof window === "undefined") {
    return;
  }
  window.localStorage.setItem(key, JSON.stringify(selection));
}

export function resolveModelSelection(
  options: AIModelOption[],
  preferred: AIModelSelection | null,
  fallback: AIModelSelection
): AIModelSelection {
  const preferredOption = preferred ? findEnabledModelOption(options, preferred) : null;
  if (preferredOption) {
    return selectionForModelOption(preferredOption, preferred);
  }
  if (preferred) {
    // Keep an explicit choice unavailable until the user picks another model.
    return preferred;
  }
  const fallbackOption = findEnabledModelOption(options, fallback);
  if (fallbackOption) {
    return selectionForModelOption(fallbackOption, fallback);
  }
  if (fallback.provider === "openai_codex" && !options.some((option) => option.enabled)) {
    return fallback;
  }
  const defaultOption =
    options.find((option) => option.default && option.enabled) ?? options.find((option) => option.enabled) ?? options[0];
  return defaultOption ? optionToSelection(defaultOption) : fallback;
}
