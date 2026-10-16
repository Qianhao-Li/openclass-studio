import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const SUPPORTED_SERVICE_TIERS = new Set(["priority"]);
const SUPPORTED_REASONING_EFFORTS = new Set([
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
]);

export default function openClassPiRuntimeSettings(pi: ExtensionAPI) {
  const serviceTier = process.env.OPENCLASS_PI_SERVICE_TIER?.trim() ?? "";
  const reasoningEffort = process.env.OPENCLASS_PI_REASONING_EFFORT?.trim() ?? "";
  if (!serviceTier && !reasoningEffort) return;
  if (serviceTier && !SUPPORTED_SERVICE_TIERS.has(serviceTier)) {
    throw new Error("OpenClass Pi runtime received an unsupported service tier");
  }
  if (reasoningEffort && !SUPPORTED_REASONING_EFFORTS.has(reasoningEffort)) {
    throw new Error("OpenClass Pi runtime received an unsupported reasoning effort");
  }

  pi.on("before_provider_request", (event) => {
    if (!event.payload || typeof event.payload !== "object" || Array.isArray(event.payload)) {
      throw new Error("OpenClass Pi runtime received an invalid provider payload");
    }
    const payload: Record<string, unknown> = { ...event.payload };
    if (serviceTier) {
      payload.service_tier = serviceTier;
    }
    if (reasoningEffort) {
      // DeepSeek Chat Completions uses top-level reasoning_effort + thinking.
      payload.thinking = { type: reasoningEffort === "none" ? "disabled" : "enabled" };
      if (reasoningEffort === "none") {
        // DeepSeek disables thinking through its toggle, not an unsupported effort.
        delete payload.reasoning_effort;
      } else {
        payload.reasoning_effort = reasoningEffort;
      }
      // OpenRouter / some Codex-shaped payloads nest effort under reasoning.
      const existingReasoning = payload.reasoning;
      if (
        existingReasoning &&
        typeof existingReasoning === "object" &&
        !Array.isArray(existingReasoning)
      ) {
        payload.reasoning = {
          ...(existingReasoning as Record<string, unknown>),
          effort: reasoningEffort,
        };
      }
    }
    return payload;
  });
}
