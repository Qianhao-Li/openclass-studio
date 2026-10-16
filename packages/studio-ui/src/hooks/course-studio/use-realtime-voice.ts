"use client";

import { useCallback, useEffect, useEffectEvent, useRef, useState, type Dispatch, type SetStateAction } from "react";

import { useStudioApi } from "../../contexts/studio-api-context";
import type { RealtimeTranscriptUpdate } from "../../components/course-studio/realtime-message-state";
import {
  PROVIDER_LABELS,
  googleRealtimeErrorMessage,
  modelButtonLabel,
  realtimeConnectionErrorMessage,
  websocketMessageText,
  type GoogleRealtimeAudioMessage,
} from "../../components/course-studio/model-catalog";
import type { AutoSaveReason } from "./use-board-draft";
import { useRealtimeLogQueue } from "../use-realtime-log-queue";
import { pcmFloatToBase64, playPcmBase64, resampleLinear } from "../../lib/realtime-audio";
import { addRealtimeBoardReference, mergeRealtimeBoardReferenceResults } from "../../lib/realtime-board-references";
import {
  applyCodexLiveTaskEvent,
  createCodexLiveTaskState,
  handleCodexLiveTaskEvent,
  resolveCodexLivePendingTask,
  shouldPublishRealtimeToolTaskStatus,
  type CodexLiveBridgeEvent,
  type CodexLiveTaskAction,
  type CodexLiveTaskState,
  type RealtimeToolStatusUpdate,
} from "./codex-live-task-ui";
import type {
  AIModelOption,
  AIModelSelection,
  Lesson,
  RealtimeToolCallResponse,
  RealtimeToolName,
  SelectionRef,
} from "../../types";

type RealtimeFunctionCall = {
  callId: string;
  name: RealtimeToolName;
  arguments: Record<string, unknown>;
};

type RealtimeInputKind = "typed" | "voice";

export type RealtimeTurnIdentity = {
  turnId: string;
  inputEventId: string;
  inputKind: RealtimeInputKind;
};

export type RealtimeTurnSnapshot = {
  identity: RealtimeTurnIdentity;
  references: SelectionRef[];
  textModel: AIModelSelection;
};

type RealtimeIdentityFactory = (prefix: string) => string;

type OpenAIRealtimeEvent = {
  type?: string;
  transcript?: string;
  delta?: string;
  item_id?: string;
  response_id?: string;
  name?: string;
  call_id?: string;
  arguments?: string;
  item?: Record<string, unknown>;
  response?: { output?: Array<Record<string, unknown>> };
};

function parseFunctionCall(item: Record<string, unknown> | undefined): RealtimeFunctionCall | null {
  if (item?.type !== "function_call" || typeof item.name !== "string" || typeof item.call_id !== "string") {
    return null;
  }
  try {
    return {
      callId: item.call_id,
      name: item.name as RealtimeToolName,
      arguments: JSON.parse(typeof item.arguments === "string" ? item.arguments : "{}") as Record<string, unknown>,
    };
  } catch {
    return null;
  }
}

export function realtimeFunctionCallsFromEvent(event: OpenAIRealtimeEvent): RealtimeFunctionCall[] {
  if (
    event.type === "response.function_call_arguments.done" &&
    typeof event.name === "string" &&
    typeof event.call_id === "string"
  ) {
    try {
      return [{
        callId: event.call_id,
        name: event.name as RealtimeToolName,
        arguments: JSON.parse(event.arguments || "{}") as Record<string, unknown>,
      }];
    } catch {
      return [];
    }
  }
  const direct = parseFunctionCall(event.item);
  if (direct) {
    return [direct];
  }
  return (event.response?.output ?? []).flatMap((item) => {
    const call = parseFunctionCall(item);
    return call ? [call] : [];
  });
}

function sendOpenAIFunctionOutput(
  dataChannel: RTCDataChannel,
  callId: string,
  output: Record<string, unknown>
) {
  if (dataChannel.readyState !== "open") {
    return;
  }
  dataChannel.send(JSON.stringify({
    type: "conversation.item.create",
    item: {
      type: "function_call_output",
      call_id: callId,
      output: JSON.stringify(output),
    },
  }));
  dataChannel.send(JSON.stringify({
    type: "response.create",
    response: { tools: [] },
  }));
}

function sendOpenAITurnDecisionRequest(dataChannel: RTCDataChannel) {
  if (dataChannel.readyState !== "open") {
    return false;
  }
  dataChannel.send(JSON.stringify({
    type: "response.create",
    response: {
      tool_choice: { type: "function", name: "run_chatbot_workflow" },
    },
  }));
  return true;
}

type UseRealtimeVoiceOptions = {
  repositoryId?: string | null;
  activeLesson: Lesson | null;
  latestAssistantMessageContent: string | null;
  selectedTextModel: AIModelSelection;
  selectedRealtimeModel: AIModelSelection;
  selectedRealtimeOption: AIModelOption | null | undefined;
  selectedRealtimeTransport: string;
  busyAction: string | null;
  setBusyAction: Dispatch<SetStateAction<string | null>>;
  setError: Dispatch<SetStateAction<string | null>>;
  flushAutoSave: (reason: AutoSaveReason) => Promise<boolean>;
  ensureDocumentHistoryBranch: () => Promise<Lesson | null>;
  isChatBusy: boolean;
  onSubmitTranscript: (message: string) => void;
  currentSelection: SelectionRef | null;
  currentSelections: SelectionRef[];
  onTranscriptUpdate: (update: RealtimeTranscriptUpdate) => void;
  onToolStatusUpdate: (update: RealtimeToolStatusUpdate) => void;
  onToolResult: (lessonId: string, result: RealtimeToolCallResponse) => void;
};

function createClientSessionId(prefix: string): string {
  return `${prefix}_${crypto.randomUUID()}`;
}

export function resolveRealtimeTurnIdentity(
  current: RealtimeTurnIdentity | null,
  inputKind: RealtimeInputKind,
  createId: RealtimeIdentityFactory = createClientSessionId
): RealtimeTurnIdentity {
  if (current) {
    return current;
  }
  return {
    turnId: createId("turn"),
    inputEventId: createId("input-event"),
    inputKind,
  };
}

export function freezeRealtimeTurnSnapshot(
  identity: RealtimeTurnIdentity,
  references: SelectionRef[],
  textModel: AIModelSelection
): RealtimeTurnSnapshot {
  if (references.length > 8) {
    throw new Error("Realtime rounds allow up to 8 frozen references");
  }
  return {
    identity: { ...identity },
    references: references.map((reference) => structuredClone(reference)),
    textModel: structuredClone(textModel),
  };
}

export function resolveRealtimeTurnSnapshot(
  currentSnapshot: RealtimeTurnSnapshot | null,
  currentIdentity: RealtimeTurnIdentity | null,
  inputKind: RealtimeInputKind,
  references: SelectionRef[],
  textModel: AIModelSelection,
  createId: RealtimeIdentityFactory = createClientSessionId
): RealtimeTurnSnapshot {
  if (currentSnapshot) {
    return currentSnapshot;
  }
  return freezeRealtimeTurnSnapshot(
    resolveRealtimeTurnIdentity(currentIdentity, inputKind, createId),
    references,
    textModel
  );
}

export function useRealtimeVoice({
  repositoryId,
  activeLesson,
  latestAssistantMessageContent,
  selectedTextModel,
  selectedRealtimeModel,
  selectedRealtimeOption,
  selectedRealtimeTransport,
  busyAction,
  setBusyAction,
  setError,
  flushAutoSave,
  ensureDocumentHistoryBranch,
  isChatBusy,
  onSubmitTranscript,
  currentSelection,
  currentSelections,
  onTranscriptUpdate,
  onToolStatusUpdate,
  onToolResult,
}: UseRealtimeVoiceOptions) {
  const api = useStudioApi();
  const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
  const realtimePeerRef = useRef<RTCPeerConnection | null>(null);
  const realtimeChannelRef = useRef<RTCDataChannel | null>(null);
  const realtimeStreamRef = useRef<MediaStream | null>(null);
  const googleRealtimeSocketRef = useRef<WebSocket | null>(null);
  const codexLiveSocketRef = useRef<WebSocket | null>(null);
  const googleAudioContextRef = useRef<AudioContext | null>(null);
  const googleAudioProcessorRef = useRef<ScriptProcessorNode | null>(null);
  const googleAudioSourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const googlePlaybackContextRef = useRef<AudioContext | null>(null);
  const googlePlaybackTimeRef = useRef(0);
  const googlePlaybackSourcesRef = useRef<Set<AudioBufferSourceNode>>(new Set());
  const googleInputTranscriptRef = useRef("");
  const googleOutputTranscriptRef = useRef("");
  const openAIResponseInProgressRef = useRef(false);
  const openAIRealtimeToolsEnabledRef = useRef(false);
  const openAIClientDelegationEnabledRef = useRef(false);
  const openAIAssistantTranscriptRef = useRef("");
  const openAIAssistantMessageIdRef = useRef<string | null>(null);
  const openAIInputTranscriptsRef = useRef(new Map<string, string>());
  const openAIProcessedToolCallsRef = useRef(new Set<string>());
  const realtimeBoardReferencesRef = useRef<SelectionRef[]>([]);
  const realtimeTurnIdentityRef = useRef<RealtimeTurnIdentity | null>(null);
  const realtimeTurnSnapshotRef = useRef<RealtimeTurnSnapshot | null>(null);
  const codexLiveDelegationTurnIdsRef = useRef(new Map<string, string>());
  const codexLiveSnapshotEventsRef = useRef(new Set<string>());
  const codexLiveTaskStateRef = useRef<CodexLiveTaskState>(createCodexLiveTaskState());
  const currentSelectionRef = useRef<SelectionRef | null>(currentSelection);
  const currentSelectionsRef = useRef<SelectionRef[]>(currentSelections);
  const selectedTextModelRef = useRef<AIModelSelection>(selectedTextModel);
  const realtimeLessonIdRef = useRef<string | null>(null);
  const realtimeClientSessionIdRef = useRef<string | null>(null);
  const realtimeLessonTitleRef = useRef<string | null>(null);
  const getRealtimeClientSessionId = useCallback(() => realtimeClientSessionIdRef.current, []);
  const getRealtimeLessonTitle = useCallback(() => realtimeLessonTitleRef.current, []);
  const { enqueueRealtimeLogEvent, flushRealtimeLogQueue, flushRealtimeLogQueueWithBeacon } = useRealtimeLogQueue({
    getClientSessionId: getRealtimeClientSessionId,
    getLessonTitle: getRealtimeLessonTitle,
  });

  const [voiceActive, setVoiceActive] = useState(false);
  const [voiceStatusText, setVoiceStatusText] = useState("Click on the microphone to connect to the real-time voice chatbot");
  const [codexLiveTaskState, setCodexLiveTaskState] = useState<CodexLiveTaskState>(createCodexLiveTaskState);

  useEffect(() => {
    currentSelectionRef.current = currentSelection;
    currentSelectionsRef.current = currentSelections;
    selectedTextModelRef.current = selectedTextModel;
    const lessonId = realtimeLessonIdRef.current;
    if (!lessonId) {
      return;
    }
    const nextReferences = currentSelections.reduce(
      (references, selection) => addRealtimeBoardReference(references, selection, lessonId),
      [] as SelectionRef[]
    );
    const previousCount = realtimeBoardReferencesRef.current.length;
    realtimeBoardReferencesRef.current = nextReferences;
    if (voiceActive && nextReferences.length !== previousCount) {
      setVoiceStatusText(
        nextReferences.length
          ? `Realtime retained ${nextReferences.length} board references`
          : "Realtime has cleared the board reference"
      );
    }
    const codexLiveSocket = codexLiveSocketRef.current;
    if (codexLiveSocket?.readyState === WebSocket.OPEN) {
      codexLiveSocket.send(JSON.stringify({
        type: "selection.update",
        selection: currentSelection,
      }));
    }
  }, [currentSelection, currentSelections, selectedTextModel, voiceActive]);

  function currentTurnIdentity() {
    const identity = resolveRealtimeTurnIdentity(realtimeTurnIdentityRef.current, "voice");
    realtimeTurnIdentityRef.current = identity;
    return identity;
  }

  function currentTurnId() {
    return currentTurnIdentity().turnId;
  }

  function beginRealtimeTurn(inputKind: RealtimeInputKind = "voice") {
    const references = currentSelectionsRef.current.length
      ? currentSelectionsRef.current
      : currentSelectionRef.current
        ? [currentSelectionRef.current]
        : [];
    const snapshot = resolveRealtimeTurnSnapshot(
      null,
      null,
      inputKind,
      references,
      selectedTextModelRef.current
    );
    realtimeTurnIdentityRef.current = snapshot.identity;
    realtimeTurnSnapshotRef.current = snapshot;
    openAIAssistantTranscriptRef.current = "";
    openAIAssistantMessageIdRef.current = null;
    return snapshot;
  }

  function ensureRealtimeTurnSnapshot(inputKind: RealtimeInputKind = "voice") {
    const references = currentSelectionsRef.current.length
      ? currentSelectionsRef.current
      : currentSelectionRef.current
        ? [currentSelectionRef.current]
        : [];
    const snapshot = resolveRealtimeTurnSnapshot(
      realtimeTurnSnapshotRef.current,
      realtimeTurnIdentityRef.current,
      inputKind,
      references,
      selectedTextModelRef.current
    );
    realtimeTurnIdentityRef.current = snapshot.identity;
    realtimeTurnSnapshotRef.current = snapshot;
    return snapshot;
  }

  function sendCodexLiveSnapshot(
    socket: WebSocket | null,
    snapshot: RealtimeTurnSnapshot | null
  ) {
    if (
      !socket ||
      !snapshot ||
      socket.readyState !== WebSocket.OPEN ||
      codexLiveSnapshotEventsRef.current.has(snapshot.identity.inputEventId)
    ) {
      return;
    }
    codexLiveSnapshotEventsRef.current.add(snapshot.identity.inputEventId);
    socket.send(JSON.stringify({
      type: "input_snapshot.update",
      turn_id: snapshot.identity.turnId,
      input_event_id: snapshot.identity.inputEventId,
      input_kind: snapshot.identity.inputKind,
      selections: snapshot.references,
      text_model: snapshot.textModel,
    }));
  }

  function delegationTurnId(delegationId: string) {
    const existing = codexLiveDelegationTurnIdsRef.current.get(delegationId);
    if (existing) {
      return existing;
    }
    const turnId = createClientSessionId("delegation-turn");
    codexLiveDelegationTurnIdsRef.current.set(delegationId, turnId);
    return turnId;
  }

  function replaceCodexLiveTaskState(nextState: CodexLiveTaskState) {
    codexLiveTaskStateRef.current = nextState;
    setCodexLiveTaskState(nextState);
    return nextState;
  }

  function applyCodexLiveBridgeEvent(payload: CodexLiveBridgeEvent) {
    return replaceCodexLiveTaskState(
      applyCodexLiveTaskEvent(codexLiveTaskStateRef.current, payload)
    );
  }

  function currentAssistantMessageId() {
    if (!openAIAssistantMessageIdRef.current) {
      openAIAssistantMessageIdRef.current = createClientSessionId("realtime-message");
    }
    return openAIAssistantMessageIdRef.current;
  }

  function stopGoogleQueuedPlayback() {
    googlePlaybackSourcesRef.current.forEach((source) => {
      try {
        source.stop();
      } catch {
        // Already ended or never started.
      }
      try {
        source.disconnect();
      } catch {
        // Already disconnected.
      }
    });
    googlePlaybackSourcesRef.current.clear();
    const playbackContext = googlePlaybackContextRef.current;
    googlePlaybackTimeRef.current = playbackContext?.currentTime ?? 0;
  }

  function queueGooglePlayback(base64: string, mimeType?: string) {
    const playbackContext = googlePlaybackContextRef.current;
    if (!playbackContext) {
      return;
    }
    const source = playPcmBase64(base64, mimeType, playbackContext, googlePlaybackTimeRef);
    googlePlaybackSourcesRef.current.add(source);
    source.addEventListener(
      "ended",
      () => {
        googlePlaybackSourcesRef.current.delete(source);
      },
      { once: true }
    );
  }

  function resetOpenAIRemoteAudioPlayback() {
    const remoteAudio = remoteAudioRef.current;
    const remoteStream = remoteAudio?.srcObject;
    if (!remoteAudio || !remoteStream) {
      return;
    }
    remoteAudio.pause();
    remoteAudio.srcObject = null;
    remoteAudio.srcObject = remoteStream;
    void remoteAudio.play().catch(() => undefined);
  }

  function disposeRealtimeSession() {
    void flushRealtimeLogQueue();
    realtimeChannelRef.current?.close();
    realtimeChannelRef.current = null;
    codexLiveSocketRef.current?.close();
    codexLiveSocketRef.current = null;
    googleRealtimeSocketRef.current?.close();
    googleRealtimeSocketRef.current = null;

    googleAudioProcessorRef.current?.disconnect();
    googleAudioProcessorRef.current = null;
    googleAudioSourceRef.current?.disconnect();
    googleAudioSourceRef.current = null;
    void googleAudioContextRef.current?.close().catch(() => undefined);
    googleAudioContextRef.current = null;
    stopGoogleQueuedPlayback();
    void googlePlaybackContextRef.current?.close().catch(() => undefined);
    googlePlaybackContextRef.current = null;
    googlePlaybackTimeRef.current = 0;
    googleInputTranscriptRef.current = "";
    googleOutputTranscriptRef.current = "";
    openAIResponseInProgressRef.current = false;
    openAIRealtimeToolsEnabledRef.current = false;
    openAIClientDelegationEnabledRef.current = false;
    openAIAssistantTranscriptRef.current = "";
    openAIAssistantMessageIdRef.current = null;
    openAIInputTranscriptsRef.current.clear();
    openAIProcessedToolCallsRef.current.clear();
    realtimeBoardReferencesRef.current = [];
    realtimeTurnIdentityRef.current = null;
    realtimeTurnSnapshotRef.current = null;
    codexLiveDelegationTurnIdsRef.current.clear();
    codexLiveSnapshotEventsRef.current.clear();
    replaceCodexLiveTaskState(createCodexLiveTaskState());

    if (realtimePeerRef.current) {
      realtimePeerRef.current.ontrack = null;
      realtimePeerRef.current.onconnectionstatechange = null;
      realtimePeerRef.current.close();
      realtimePeerRef.current = null;
    }

    realtimeStreamRef.current?.getTracks().forEach((track) => track.stop());
    realtimeStreamRef.current = null;

    if (remoteAudioRef.current) {
      remoteAudioRef.current.pause();
      remoteAudioRef.current.srcObject = null;
    }

    realtimeLessonIdRef.current = null;
    realtimeClientSessionIdRef.current = null;
    realtimeLessonTitleRef.current = null;
  }

  function stopRealtimeSession(statusText = "Voice Chatbot is disconnected") {
    disposeRealtimeSession();
    window.speechSynthesis?.cancel();
    setVoiceActive(false);
    setVoiceStatusText(statusText);
    setBusyAction((current) => (current === "voice-connect" ? null : current));
  }

  const stopRealtimeSessionEvent = useEffectEvent((statusText: string) => {
    stopRealtimeSession(statusText);
  });

  function speakControlledChatbotMessage(content: string) {
    if (typeof window === "undefined" || !("speechSynthesis" in window)) {
      return;
    }
    const text = content.trim();
    if (!text) {
      return;
    }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.lang = "zh-CN";
    utterance.rate = 1;
    utterance.pitch = 1;
    window.speechSynthesis.speak(utterance);
  }

  function handleRealtimeUserTranscript(lessonId: string, transcript: string, eventType: string) {
    const normalized = transcript.trim();
    if (!normalized) {
      return;
    }
    const turnIdentity = currentTurnIdentity();
    const turnId = turnIdentity.turnId;
    const messageId = turnIdentity.inputEventId;
    if (!openAIClientDelegationEnabledRef.current) {
      enqueueRealtimeLogEvent(lessonId, "user", eventType, normalized, {
        clientEventId: messageId,
        turnId,
      });
    }
    onTranscriptUpdate({
      lessonId,
      turnId,
      messageId,
      role: "user",
      text: normalized,
      final: true,
    });
    if (openAIRealtimeToolsEnabledRef.current) {
      const dataChannel = realtimeChannelRef.current;
      if (!dataChannel || !sendOpenAITurnDecisionRequest(dataChannel)) {
        setVoiceStatusText("Realtime round decision channel is not ready");
        return;
      }
      setVoiceStatusText("Realtime is determining the needs of this round and the path written on the board");
      return;
    }
    if (openAIClientDelegationEnabledRef.current) {
      setVoiceStatusText("Codex Live is being handed over to Chatbot workflow processing");
      return;
    }
    if (isChatBusy) {
      setVoiceStatusText("The previous voice sentence is being processed, please wait a moment.");
      return;
    }
    onSubmitTranscript(normalized);
  }

  function flushGoogleRealtimeTranscripts(lessonId: string) {
    const userTranscript = googleInputTranscriptRef.current.trim();
    const assistantTranscript = googleOutputTranscriptRef.current.trim();
    if (userTranscript) {
      handleRealtimeUserTranscript(lessonId, userTranscript, "google.input_transcription");
      googleInputTranscriptRef.current = "";
    }
    if (assistantTranscript) {
      const turnId = currentTurnId();
      const messageId = currentAssistantMessageId();
      enqueueRealtimeLogEvent(lessonId, "assistant", "google.output_transcription", assistantTranscript, {
        clientEventId: messageId,
        turnId,
      });
      onTranscriptUpdate({
        lessonId,
        turnId,
        messageId,
        role: "assistant",
        text: assistantTranscript,
        final: true,
      });
      googleOutputTranscriptRef.current = "";
    }
    realtimeTurnIdentityRef.current = null;
    realtimeTurnSnapshotRef.current = null;
    openAIAssistantMessageIdRef.current = null;
  }

  function beginGoogleAudioStreaming(socket: WebSocket, mediaStream: MediaStream, audioContext: AudioContext) {
    const source = audioContext.createMediaStreamSource(mediaStream);
    const processor = audioContext.createScriptProcessor(4096, 1, 1);
    source.connect(processor);
    processor.connect(audioContext.destination);
    googleAudioSourceRef.current = source;
    googleAudioProcessorRef.current = processor;
    processor.onaudioprocess = (event) => {
      if (socket.readyState !== WebSocket.OPEN) {
        return;
      }
      const input = event.inputBuffer.getChannelData(0);
      const resampled = resampleLinear(input, audioContext.sampleRate, 16000);
      socket.send(
        JSON.stringify({
          realtimeInput: {
            audio: {
              mimeType: "audio/pcm;rate=16000",
              data: pcmFloatToBase64(resampled),
            },
          },
        })
      );
    };
  }

  function handleGoogleRealtimeMessage(message: GoogleRealtimeAudioMessage) {
    const lessonId = realtimeLessonIdRef.current;
    if (!lessonId) {
      return;
    }
    const serverContent = message.serverContent;
    if (!serverContent) {
      return;
    }
    const inputText = serverContent.inputTranscription?.text;
    if (inputText) {
      currentTurnId();
      googleInputTranscriptRef.current += inputText;
    }
    if (serverContent.interrupted) {
      beginRealtimeTurn();
      stopGoogleQueuedPlayback();
      googleOutputTranscriptRef.current = "";
      setVoiceStatusText("Interruption detected, the previous reply has been stopped.");
    }
    const outputText = serverContent.outputTranscription?.text;
    if (outputText && !serverContent.interrupted) {
      googleOutputTranscriptRef.current += outputText;
    }
    serverContent.modelTurn?.parts?.forEach((part) => {
      const inlineData = part.inlineData;
      if (!inlineData?.data || serverContent.interrupted) {
        return;
      }
      queueGooglePlayback(inlineData.data, inlineData.mimeType);
    });
    if (serverContent.turnComplete) {
      flushGoogleRealtimeTranscripts(lessonId);
    }
  }

  async function startGoogleRealtimeSession(lesson: Lesson, mediaStream: MediaStream, clientSessionId: string) {
    const session = await api.createGoogleRealtimeSession(lesson.id, {
      latest_assistant_message: latestAssistantMessageContent,
      client_session_id: clientSessionId,
      realtime_model: selectedRealtimeModel,
    });
    const audioContext = new AudioContext();
    const playbackContext = new AudioContext();
    googleAudioContextRef.current = audioContext;
    googlePlaybackContextRef.current = playbackContext;
    googlePlaybackTimeRef.current = playbackContext.currentTime;
    await audioContext.resume();
    await playbackContext.resume();

    const socket = new WebSocket(api.webSocketUrl(session.websocket_url));
    googleRealtimeSocketRef.current = socket;
    await new Promise<void>((resolve, reject) => {
      let streamingStarted = false;
      let settled = false;
      const resolveStart = () => {
        if (settled) {
          return;
        }
        settled = true;
        resolve();
      };
      const rejectStart = (message: string) => {
        if (settled) {
          return;
        }
        settled = true;
        reject(new Error(message));
      };
      socket.onopen = () => {
        socket.send(JSON.stringify(session.setup));
      };
      socket.onerror = () => {
        rejectStart("Google Gemini Live WebSocket connection failed");
      };
      socket.onclose = (event) => {
        if (!streamingStarted) {
          rejectStart(
            `Google Gemini Live WebSocket closed before initialization (${event.code}${event.reason ? `: ${event.reason}` : ""})`
          );
        }
        if (googleRealtimeSocketRef.current === socket) {
          stopRealtimeSession("Google Gemini Live session ended");
        }
      };
      socket.onmessage = (event) => {
        void (async () => {
          try {
            const messageText = await websocketMessageText(event.data);
            const payload = JSON.parse(messageText) as GoogleRealtimeAudioMessage;
            if (payload.error) {
              const message = googleRealtimeErrorMessage(payload.error);
              if (!streamingStarted) {
                rejectStart(message);
                return;
              }
              stopRealtimeSession("Google Gemini Live session ended");
              setError(message);
              return;
            }
            if (payload.setupComplete && !streamingStarted) {
              streamingStarted = true;
              beginGoogleAudioStreaming(socket, mediaStream, audioContext);
              setVoiceActive(true);
              setBusyAction((current) => (current === "voice-connect" ? null : current));
              setVoiceStatusText(`Google Gemini Live connected with voice ${session.voice}`);
              resolveStart();
              return;
            }
            handleGoogleRealtimeMessage(payload);
          } catch {
            // ignore malformed realtime events
          }
        })();
      };
    });
  }

  async function startCodexLiveBridge(websocketUrl: string) {
    const socket = new WebSocket(api.webSocketUrl(websocketUrl));
    codexLiveSocketRef.current = socket;
    await new Promise<void>((resolve, reject) => {
      let settled = false;
      const timeoutId = window.setTimeout(() => {
        if (!settled) {
          settled = true;
          socket.close();
          reject(new Error("Codex Live Chatbot workflow channel connection timeout"));
        }
      }, 20_000);
      const resolveStart = () => {
        if (settled) {
          return;
        }
        settled = true;
        window.clearTimeout(timeoutId);
        resolve();
      };
      const rejectStart = (message: string) => {
        if (settled) {
          return;
        }
        settled = true;
        window.clearTimeout(timeoutId);
        reject(new Error(message));
      };
      socket.onopen = () => {
        socket.send(JSON.stringify({
          type: "selection.update",
          selection: currentSelectionRef.current,
        }));
      };
      socket.onerror = () => {
        rejectStart("Codex Live Chatbot workflow channel connection failed");
      };
      socket.onclose = (event) => {
        if (!settled) {
          rejectStart(`Codex Live Chatbot workflow channel closed before initialization (${event.code})`);
        }
        if (codexLiveSocketRef.current === socket) {
          codexLiveSocketRef.current = null;
          stopRealtimeSession("Codex Live Chatbot workflow channel ended");
        }
      };
      socket.onmessage = (event) => {
        void (async () => {
          try {
            const messageText = await websocketMessageText(event.data);
            const payload = JSON.parse(messageText) as CodexLiveBridgeEvent;
            const lessonId = realtimeLessonIdRef.current;
            if (payload.type === "codex_live.ready") {
              resolveStart();
              return;
            }
            if (!lessonId) {
              return;
            }
            if (payload.type === "codex_live.transcript.delta" && payload.role && payload.text) {
              if (payload.role === "user") {
                const snapshot = ensureRealtimeTurnSnapshot();
                sendCodexLiveSnapshot(
                  codexLiveSocketRef.current,
                  snapshot
                );
                const transcriptKey = "codex-live-user";
                const transcript = `${openAIInputTranscriptsRef.current.get(transcriptKey) ?? ""}${payload.text}`;
                openAIInputTranscriptsRef.current.set(transcriptKey, transcript);
                const turnId = snapshot.identity.turnId;
                onTranscriptUpdate({
                  lessonId,
                  turnId,
                  messageId: `realtime:${turnId}:user`,
                  role: "user",
                  text: transcript,
                  final: false,
                });
              } else {
                openAIAssistantTranscriptRef.current += payload.text;
                onTranscriptUpdate({
                  lessonId,
                  turnId: currentTurnId(),
                  messageId: currentAssistantMessageId(),
                  role: "assistant",
                  text: openAIAssistantTranscriptRef.current,
                  final: false,
                });
              }
              return;
            }
            if (payload.type === "codex_live.transcript.done" && payload.role) {
              const transcript = (payload.text ?? "").trim();
              if (payload.role === "user") {
                const snapshot = ensureRealtimeTurnSnapshot();
                sendCodexLiveSnapshot(codexLiveSocketRef.current, snapshot);
                openAIInputTranscriptsRef.current.delete("codex-live-user");
                handleRealtimeUserTranscript(lessonId, transcript, payload.type);
              } else if (transcript) {
                const turnId = currentTurnId();
                const messageId = currentAssistantMessageId();
                onTranscriptUpdate({
                  lessonId,
                  turnId,
                  messageId,
                  role: "assistant",
                  text: transcript,
                  final: true,
                });
                openAIAssistantTranscriptRef.current = "";
                openAIAssistantMessageIdRef.current = null;
                realtimeTurnIdentityRef.current = null;
                realtimeTurnSnapshotRef.current = null;
              }
              return;
            }
            if (handleCodexLiveTaskEvent(payload, {
              lessonId,
              applyTaskEvent: applyCodexLiveBridgeEvent,
              delegationTurnId,
              onToolStatusUpdate,
              onToolResult,
              setVoiceStatusText,
              setError,
              logWorkflowStarted: (delegationId, turnId) =>
                enqueueRealtimeLogEvent(lessonId, "tool", "codex_live.workflow.started", delegationId, { turnId }),
            })) {
              return;
            }
          } catch {
            // Ignore malformed bridge events while keeping the media session alive.
          }
        })();
      };
    });
  }

  async function handleVoiceToggle() {
    if (typeof window === "undefined") {
      return;
    }
    if (voiceActive || busyAction === "voice-connect") {
      stopRealtimeSession("Voice Chatbot has been manually disconnected");
      return;
    }
    if (!activeLesson) {
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia) {
      setError("The current browser cannot access the microphone. Please use a microphone-enabled browser and open the page via localhost or HTTPS.");
      return;
    }
    if (selectedRealtimeOption && !selectedRealtimeOption.enabled) {
      setError(`No realtime voice API key is configured for ${PROVIDER_LABELS[selectedRealtimeModel.provider]}.`);
      return;
    }
    if (!(await flushAutoSave("voice"))) {
      return;
    }
    if (!(await ensureDocumentHistoryBranch())) {
      return;
    }

    setBusyAction("voice-connect");
    const realtimeLabel = modelButtonLabel(selectedRealtimeOption ?? null, selectedRealtimeModel);
    setVoiceStatusText(`Connecting to ${realtimeLabel}…`);
    setError(null);

    try {
      const mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
      realtimeStreamRef.current = mediaStream;

      const clientSessionId = createClientSessionId("realtime");
      realtimeLessonIdRef.current = activeLesson.id;
      realtimeClientSessionIdRef.current = clientSessionId;
      realtimeLessonTitleRef.current = activeLesson.title;
      realtimeBoardReferencesRef.current = currentSelections.reduce(
        (references, selection) => addRealtimeBoardReference(references, selection, activeLesson.id),
        [] as SelectionRef[]
      );

      if (selectedRealtimeTransport === "gemini_live_websocket" || selectedRealtimeModel.provider === "google") {
        await startGoogleRealtimeSession(activeLesson, mediaStream, clientSessionId);
        return;
      }

      const peerConnection = new RTCPeerConnection();
      realtimePeerRef.current = peerConnection;

      mediaStream.getTracks().forEach((track) => {
        peerConnection.addTrack(track, mediaStream);
      });

      peerConnection.ontrack = (event) => {
        const [remoteStream] = event.streams;
        if (remoteAudioRef.current && remoteStream) {
          remoteAudioRef.current.srcObject = remoteStream;
          void remoteAudioRef.current.play().catch(() => undefined);
        }
      };

      peerConnection.onconnectionstatechange = () => {
        if (peerConnection.connectionState === "connected") {
          setVoiceActive(true);
          setVoiceStatusText(`${realtimeLabel} connected. Speech will enter the Chatbot workflow first.`);
          setBusyAction((current) => (current === "voice-connect" ? null : current));
          return;
        }
        if (
          peerConnection.connectionState === "failed" ||
          peerConnection.connectionState === "closed" ||
          peerConnection.connectionState === "disconnected"
        ) {
          stopRealtimeSession("Voice session ended");
        }
      };

      const dataChannel = peerConnection.createDataChannel("oai-events");
      realtimeChannelRef.current = dataChannel;
      dataChannel.onmessage = (messageEvent) => {
        let publishUnexpectedToolError = true;
        void (async () => {
          try {
            const payload = JSON.parse(messageEvent.data) as OpenAIRealtimeEvent;
          if (payload.type === "response.created") {
            openAIResponseInProgressRef.current = true;
            openAIAssistantMessageIdRef.current = createClientSessionId("realtime-message");
          }
          if (
            payload.type === "response.done" ||
            payload.type === "response.audio.done" ||
            payload.type === "response.output_audio.done" ||
            payload.type === "response.output_text.done"
          ) {
            openAIResponseInProgressRef.current = false;
          }
          if (payload.type === "input_audio_buffer.speech_started") {
            const snapshot = openAIClientDelegationEnabledRef.current
              ? ensureRealtimeTurnSnapshot()
              : beginRealtimeTurn();
            sendCodexLiveSnapshot(codexLiveSocketRef.current, snapshot);
            if (openAIResponseInProgressRef.current && dataChannel.readyState === "open") {
              dataChannel.send(JSON.stringify({ type: "response.cancel" }));
              openAIResponseInProgressRef.current = false;
            }
            openAIAssistantTranscriptRef.current = "";
            resetOpenAIRemoteAudioPlayback();
          }
          const lessonId = realtimeLessonIdRef.current;
          if (!lessonId || !payload.type) {
            return;
          }
          const functionCalls = realtimeFunctionCallsFromEvent(payload);
          for (const functionCall of functionCalls) {
            if (openAIProcessedToolCallsRef.current.has(functionCall.callId)) {
              continue;
            }
            openAIProcessedToolCallsRef.current.add(functionCall.callId);
            const turnIdentity = currentTurnIdentity();
            const turnSnapshot = realtimeTurnSnapshotRef.current;
            const turnId = turnIdentity.turnId;
            const providerReference = payload.response_id ?? payload.item_id ?? functionCall.callId;
            const toolLabel = functionCall.name === "read_board_context" ? "Locating and reading board content" : "Being handed over to Chatbot workflow for processing";
            const publishPreRouteTaskStatus = shouldPublishRealtimeToolTaskStatus(
              functionCall.name
            );
            publishUnexpectedToolError = publishPreRouteTaskStatus;
            setVoiceStatusText(toolLabel);
            if (publishPreRouteTaskStatus) {
              onToolStatusUpdate({ lessonId, turnId, label: toolLabel, status: "pending" });
            }
            enqueueRealtimeLogEvent(lessonId, "tool", payload.type, `${functionCall.name} (${functionCall.callId})`, {
              turnId,
            });
            const clientSessionId = realtimeClientSessionIdRef.current;
            if (!clientSessionId) {
              const message = "Realtime client session ID has expired";
              if (publishPreRouteTaskStatus) {
                onToolStatusUpdate({ lessonId, turnId, label: message, status: "error" });
              }
              sendOpenAIFunctionOutput(dataChannel, functionCall.callId, { status: "error", message });
              continue;
            }
            if (
              !turnSnapshot ||
              turnSnapshot.identity.inputEventId !== turnIdentity.inputEventId
            ) {
              const message = "Realtime round missing input snapshot frozen on commit";
              if (publishPreRouteTaskStatus) {
                onToolStatusUpdate({ lessonId, turnId, label: message, status: "error" });
              }
              sendOpenAIFunctionOutput(dataChannel, functionCall.callId, {
                status: "error",
                message,
              });
              continue;
            }
            let toolResult: RealtimeToolCallResponse;
            try {
              const frozenArguments = {
                ...functionCall.arguments,
                __openclass_turn_snapshot: {
                  references: turnSnapshot.references,
                  text_model: turnSnapshot.textModel,
                },
              };
              const useAccumulatedReferences =
                functionCall.name === "read_board_context" &&
                functionCall.arguments.mode === "current_selection" &&
                turnSnapshot.references.length > 1;
              if (useAccumulatedReferences) {
                const referenceResults = await Promise.all(
                  turnSnapshot.references.map((selection, index) =>
                    api.callRealtimeTool(lessonId, {
                      client_session_id: clientSessionId,
                      turn_id: turnId,
                      input_event_id: turnIdentity.inputEventId,
                      input_kind: turnIdentity.inputKind,
                      provider_reference: providerReference,
                      call_id: `${functionCall.callId}_reference_${index + 1}`,
                      name: functionCall.name,
                      arguments: frozenArguments,
                      selection,
                    }, repositoryId)
                  )
                );
                toolResult = mergeRealtimeBoardReferenceResults(referenceResults);
              } else {
                toolResult = await api.callRealtimeTool(lessonId, {
                  client_session_id: clientSessionId,
                  turn_id: turnId,
                  input_event_id: turnIdentity.inputEventId,
                  input_kind: turnIdentity.inputKind,
                  provider_reference: providerReference,
                  call_id: functionCall.callId,
                  name: functionCall.name,
                  arguments: frozenArguments,
                  selection: turnSnapshot.references[0] ?? null,
                }, repositoryId);
              }
            } catch (toolError) {
              const message = toolError instanceof Error ? toolError.message : "Realtime tool execution failed";
              if (publishPreRouteTaskStatus) {
                onToolStatusUpdate({ lessonId, turnId, label: message, status: "error" });
              }
              setVoiceStatusText(message);
              sendOpenAIFunctionOutput(dataChannel, functionCall.callId, { status: "error", message });
              continue;
            }
            onToolResult(lessonId, toolResult);
            const modelStatus = toolResult.model_output.status;
            const succeeded = toolResult.status === "ok" && modelStatus === "ok";
            const referenceCount = Number(toolResult.model_output.reference_count ?? 0);
            const completedLabel = functionCall.name === "read_board_context"
              ? referenceCount > 1
                ? `${referenceCount} board references ready`
                : "Board context is ready"
              : "Chatbot workflow completed";
            const failedLabel = toolResult.status === "ok" && modelStatus === "not_found"
              ? "No clear board content range has been located."
              : "Realtime tool execution failed";
            const publishRoutedTaskStatus = shouldPublishRealtimeToolTaskStatus(
              functionCall.name,
              toolResult
            );
            publishUnexpectedToolError = publishRoutedTaskStatus;
            if (publishRoutedTaskStatus) {
              onToolStatusUpdate({
                lessonId,
                turnId,
                label: succeeded ? completedLabel : failedLabel,
                status: succeeded ? "completed" : "error",
              });
            }
            setVoiceStatusText(succeeded ? `${completedLabel}; Realtime is responding` : failedLabel);
            sendOpenAIFunctionOutput(dataChannel, functionCall.callId, toolResult.model_output);
          }
          const inputItemId = payload.item_id ?? currentTurnId();
          if (payload.type === "conversation.item.input_audio_transcription.delta" && payload.delta) {
            const transcript = `${openAIInputTranscriptsRef.current.get(inputItemId) ?? ""}${payload.delta}`;
            openAIInputTranscriptsRef.current.set(inputItemId, transcript);
            const turnId = currentTurnId();
            onTranscriptUpdate({
              lessonId,
              turnId,
              messageId: `realtime:${turnId}:user`,
              role: "user",
              text: transcript,
              final: false,
            });
          }
          if (
            (payload.type === "conversation.item.input_audio_transcription.completed" ||
              payload.type === "conversation.item.input_audio_transcription.done")
          ) {
            const transcript = payload.transcript ?? openAIInputTranscriptsRef.current.get(inputItemId) ?? "";
            openAIInputTranscriptsRef.current.delete(inputItemId);
            handleRealtimeUserTranscript(lessonId, transcript, payload.type);
          }
          if (payload.type === "response.output_audio_transcript.delta" && payload.delta) {
            openAIAssistantTranscriptRef.current += payload.delta;
            onTranscriptUpdate({
              lessonId,
              turnId: currentTurnId(),
              messageId: currentAssistantMessageId(),
              role: "assistant",
              text: openAIAssistantTranscriptRef.current,
              final: false,
            });
          }
          if (payload.type === "response.output_text.delta" && payload.delta) {
            openAIAssistantTranscriptRef.current += payload.delta;
            onTranscriptUpdate({
              lessonId,
              turnId: currentTurnId(),
              messageId: currentAssistantMessageId(),
              role: "assistant",
              text: openAIAssistantTranscriptRef.current,
              final: false,
            });
          }
          if (
            payload.type === "response.audio_transcript.done" ||
            payload.type === "response.output_audio_transcript.done" ||
            payload.type === "response.output_text.done"
          ) {
            const transcript = payload.transcript ?? openAIAssistantTranscriptRef.current;
            const turnId = currentTurnId();
            const messageId = currentAssistantMessageId();
            enqueueRealtimeLogEvent(lessonId, "assistant", payload.type, transcript, {
              clientEventId: messageId,
              turnId,
            });
            onTranscriptUpdate({ lessonId, turnId, messageId, role: "assistant", text: transcript, final: true });
            openAIAssistantTranscriptRef.current = "";
            openAIAssistantMessageIdRef.current = null;
          }
          } catch (toolError) {
            const lessonId = realtimeLessonIdRef.current;
            if (lessonId && publishUnexpectedToolError) {
              onToolStatusUpdate({
                lessonId,
                turnId: currentTurnId(),
                label: toolError instanceof Error ? toolError.message : "Realtime tool execution failed",
                status: "error",
              });
            }
            setError(toolError instanceof Error ? toolError.message : "Realtime tool execution failed");
          }
        })();
      };

      const offer = await peerConnection.createOffer();
      await peerConnection.setLocalDescription(offer);

      const realtimeResponse = await api.connectRealtime(activeLesson.id, {
        offer_sdp: offer.sdp ?? "",
        latest_assistant_message: latestAssistantMessageContent,
        client_session_id: clientSessionId,
        realtime_model: selectedRealtimeModel,
        selection: currentSelection,
      }, repositoryId);
      if (realtimeResponse.client_session_id) {
        realtimeClientSessionIdRef.current = realtimeResponse.client_session_id;
      }
      openAIRealtimeToolsEnabledRef.current = Boolean(realtimeResponse.tools_enabled);
      openAIClientDelegationEnabledRef.current = Boolean(realtimeResponse.client_delegation_enabled);

      if (realtimeResponse.client_delegation_enabled) {
        if (!realtimeResponse.delegation_websocket_url) {
          throw new Error("Codex Live response is missing Chatbot workflow channel");
        }
        await startCodexLiveBridge(realtimeResponse.delegation_websocket_url);
      }

      await peerConnection.setRemoteDescription({
        type: "answer",
        sdp: realtimeResponse.answer_sdp,
      });

      setVoiceStatusText(
        `${PROVIDER_LABELS[realtimeResponse.provider]} ${realtimeResponse.model} ready${
          realtimeResponse.tools_enabled || realtimeResponse.client_delegation_enabled
            ? ", can read board content and run Chatbot workflow"
            : ", being transcribed under controlled"
        }`
      );
    } catch (voiceError) {
      stopRealtimeSession("Voice connection failed");
      setError(realtimeConnectionErrorMessage(voiceError, selectedRealtimeModel));
    }
  }

  function sendRealtimeText(message: string) {
    const normalized = message.trim();
    const lessonId = realtimeLessonIdRef.current;
    const dataChannel = realtimeChannelRef.current;
    if (!normalized || !lessonId) {
      return false;
    }
    const codexLiveSocket = codexLiveSocketRef.current;
    const usesClientDelegation = openAIClientDelegationEnabledRef.current;
    if (usesClientDelegation) {
      if (!codexLiveSocket || codexLiveSocket.readyState !== WebSocket.OPEN) {
        return false;
      }
    } else if (!dataChannel || dataChannel.readyState !== "open") {
      return false;
    }
    let turnSnapshot: RealtimeTurnSnapshot;
    try {
      turnSnapshot = beginRealtimeTurn("typed");
    } catch (snapshotError) {
      setError(
        snapshotError instanceof Error
          ? snapshotError.message
          : "Realtime turn input snapshot is invalid"
      );
      return false;
    }
    const turnIdentity = turnSnapshot.identity;
    const turnId = turnIdentity.turnId;
    const messageId = turnIdentity.inputEventId;
    onTranscriptUpdate({ lessonId, turnId, messageId, role: "user", text: normalized, final: true });
    if (!usesClientDelegation) {
      enqueueRealtimeLogEvent(lessonId, "user", "conversation.item.input_text", normalized, {
        clientEventId: messageId,
        turnId,
      });
    }
    if (usesClientDelegation) {
      codexLiveSocket?.send(JSON.stringify({
        type: "input_text",
        text: normalized,
        client_session_id: realtimeClientSessionIdRef.current,
        turn_id: turnId,
        input_event_id: messageId,
        input_kind: turnIdentity.inputKind,
        selections: turnSnapshot.references,
        text_model: turnSnapshot.textModel,
      }));
      setVoiceStatusText("Codex Live is processing text messages");
      return true;
    }
    dataChannel?.send(JSON.stringify({
      type: "conversation.item.create",
      event_id: messageId,
      item: {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: normalized }],
      },
    }));
    if (openAIRealtimeToolsEnabledRef.current) {
      sendOpenAITurnDecisionRequest(dataChannel as RTCDataChannel);
    } else {
      dataChannel?.send(JSON.stringify({ type: "response.create" }));
    }
    setVoiceStatusText("Realtime is processing text messages");
    return true;
  }

  function resolveCodexLiveTask(delegationId: string, action: CodexLiveTaskAction) {
    const socket = codexLiveSocketRef.current;
    if (!delegationId || !socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    socket.send(JSON.stringify({ type: "delegation.resolve", delegation_id: delegationId, action }));
    replaceCodexLiveTaskState(
      resolveCodexLivePendingTask(codexLiveTaskStateRef.current, delegationId, action)
    );
    setVoiceStatusText(
      action === "dismiss"
        ? "This paragraph has been ignored"
        : "Updating task schedule"
    );
    return true;
  }

  const scheduleRealtimeLogFlushEffectEvent = useEffectEvent(() => {
    void flushRealtimeLogQueue();
  });

  const flushRealtimeLogQueueWithBeaconEffectEvent = useEffectEvent(() => {
    flushRealtimeLogQueueWithBeacon();
  });

  const disposeRealtimeSessionEffectEvent = useEffectEvent(() => {
    disposeRealtimeSession();
  });

  useEffect(() => {
    return () => {
      flushRealtimeLogQueueWithBeaconEffectEvent();
      disposeRealtimeSessionEffectEvent();
    };
  }, []);

  useEffect(() => {
    const intervalId = window.setInterval(() => {
      scheduleRealtimeLogFlushEffectEvent();
    }, 2000);

    function handlePageHide() {
      flushRealtimeLogQueueWithBeaconEffectEvent();
    }

    window.addEventListener("pagehide", handlePageHide);
    window.addEventListener("beforeunload", handlePageHide);
    return () => {
      window.clearInterval(intervalId);
      window.removeEventListener("pagehide", handlePageHide);
      window.removeEventListener("beforeunload", handlePageHide);
    };
  }, []);

  useEffect(() => {
    if (!realtimeLessonIdRef.current || realtimeLessonIdRef.current === activeLesson?.id) {
      return;
    }
    stopRealtimeSessionEvent("Courses have been switched and the voice session has been automatically disconnected");
  }, [activeLesson?.id]);

  return {
    remoteAudioRef,
    voiceActive,
    voiceStatusText,
    setVoiceStatusText,
    handleVoiceToggle,
    stopRealtimeSession,
    speakControlledChatbotMessage,
    sendRealtimeText,
    codexLiveTaskState,
    resolveCodexLiveTask,
  };
}
