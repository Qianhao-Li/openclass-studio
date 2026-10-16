"use client";
import { useStudioApi } from "../../contexts/studio-api-context";
import { useStudioHost } from "../../contexts/studio-host-context";

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";

import type { ChatMessage } from "../../components/course-studio/history-utils";
import {
  startCodexLiveSpeech,
  type CodexLiveSpeechPlayback,
} from "../../lib/codex-live-speech";
import {
  getSpeechOptions,
  synthesizeSpeech,
  type SpeechOptionsResponse,
} from "../../lib/speech-api";
import { prepareSpeechText } from "../../lib/speech-text";

const AUTO_SPEECH_STORAGE_KEY = "openclass:studio:auto-speak-chat";
const AUTO_SPEECH_CHANGE_EVENT = "openclass:auto-speech-change";
const SPEECH_VOICE_STORAGE_KEY = "openclass:studio:speech-voice";
const SPEECH_RATE_STORAGE_KEY = "openclass:studio:speech-rate";

const DEFAULT_SPEECH_OPTIONS: SpeechOptionsResponse = {
  provider: "openai_codex",
  model: "gpt-live-1-codex",
  default_voice: "cove",
  voices: [
    {
      id: "cove",
      label: "Cove",
      description: "Codex Live real-time sounds",
    },
  ],
  minimum_speech_rate: 0,
  maximum_speech_rate: 0,
  default_speech_rate: 0,
  delivery: "buffered_live_audio",
  supports_speech_rate: false,
  supports_seek: true,
};

type SpeechPlaybackStatus = "idle" | "loading" | "playing" | "paused" | "error";

type UseChatSpeechOptions = {
  lessonId: string | null;
  messages: ChatMessage[];
};

function subscribeToAutoSpeechPreference(callback: () => void) {
  window.addEventListener("storage", callback);
  window.addEventListener(AUTO_SPEECH_CHANGE_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(AUTO_SPEECH_CHANGE_EVENT, callback);
  };
}

function readAutoSpeechPreference() {
  try {
    const storedValue = window.localStorage.getItem(AUTO_SPEECH_STORAGE_KEY);
    return storedValue === null ? true : storedValue === "true";
  } catch {
    return true;
  }
}

export function useChatSpeech({ lessonId, messages }: UseChatSpeechOptions) {
  const speechAvailable = useStudioHost().capabilities?.speech !== false;
  const api = useStudioApi();
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioUrlRef = useRef<string | null>(null);
  const requestRef = useRef<AbortController | null>(null);
  const livePlaybackRef = useRef<CodexLiveSpeechPlayback | null>(null);
  const requestSequenceRef = useRef(0);
  const trackedLessonIdRef = useRef<string | null>(null);
  const latestSeenAssistantIdRef = useRef<string | null>(null);
  const autoSpeechPreference = useSyncExternalStore(
    subscribeToAutoSpeechPreference,
    readAutoSpeechPreference,
    () => true
  );
  const [status, setStatus] = useState<SpeechPlaybackStatus>("idle");
  const [statusMessage, setStatusMessage] = useState("Waiting for new AI reply");
  const autoSpeakEnabled = speechAvailable && autoSpeechPreference;
  const [speechOptions, setSpeechOptions] = useState({ ...DEFAULT_SPEECH_OPTIONS, available: speechAvailable });
  const [selectedVoice, setSelectedVoiceState] = useState(DEFAULT_SPEECH_OPTIONS.default_voice);
  const [speechRate, setSpeechRateState] = useState(DEFAULT_SPEECH_OPTIONS.default_speech_rate);
  const [currentModel, setCurrentModel] = useState(DEFAULT_SPEECH_OPTIONS.model);
  const [currentSpeechText, setCurrentSpeechText] = useState("");
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  const latestAssistantMessage = useMemo(
    () =>
      [...messages]
        .reverse()
        .find((message) => message.role === "assistant" && message.status === "ready" && message.content.trim()),
    [messages]
  );

  const releaseAudio = useCallback(() => {
    livePlaybackRef.current?.stop();
    livePlaybackRef.current = null;
    const audio = audioRef.current;
    if (audio) {
      audio.onplay = null;
      audio.onloadedmetadata = null;
      audio.ondurationchange = null;
      audio.ontimeupdate = null;
      audio.onended = null;
      audio.onerror = null;
      audio.pause();
      audio.removeAttribute("src");
      audio.load();
      audioRef.current = null;
    }
    if (audioUrlRef.current) {
      URL.revokeObjectURL(audioUrlRef.current);
      audioUrlRef.current = null;
    }
  }, []);

  const stopSpeech = useCallback(() => {
    requestSequenceRef.current += 1;
    requestRef.current?.abort();
    requestRef.current = null;
    releaseAudio();
    setCurrentTime(0);
    setDuration(0);
    setStatus("idle");
    setStatusMessage(autoSpeakEnabled ? "Waiting for new AI reply" : "Auto-announcement is turned off");
  }, [autoSpeakEnabled, releaseAudio]);

  const speakMessage = useCallback(
    async (content: string) => {
      if (!speechAvailable) return;
      const speechText = prepareSpeechText(content);
      if (!speechText) {
        return;
      }

      const requestSequence = requestSequenceRef.current + 1;
      requestSequenceRef.current = requestSequence;
      requestRef.current?.abort();
      releaseAudio();
      const controller = new AbortController();
      requestRef.current = controller;
      setCurrentSpeechText(speechText);
      setCurrentTime(0);
      setDuration(0);
      setStatus("loading");
      setStatusMessage(
        speechOptions.delivery === "buffered_live_audio" ||
        speechOptions.delivery === "realtime_audio"
          ? "Codex Live is generating draggable audio…"
          : "TTS is generating audio..."
      );

      try {
        if (
          speechOptions.delivery === "buffered_live_audio" ||
          speechOptions.delivery === "realtime_audio"
        ) {
          if (!lessonId) {
            throw new Error("Please open the course first and then use Codex Live to broadcast");
          }
          const playback = await startCodexLiveSpeech({
            client: api,
            lessonId,
            text: speechText,
            voice: selectedVoice,
            signal: controller.signal,
            onPlaying: (audio, model, voice) => {
              if (requestSequenceRef.current !== requestSequence) {
                return;
              }
              audioRef.current = audio;
              setCurrentModel(model);
              setStatus("playing");
              setStatusMessage(`Codex Live speaking · ${model} · ${voice}`);
            },
            onStatus: (message) => {
              if (requestSequenceRef.current === requestSequence) {
                setStatusMessage(message);
              }
            },
            onElapsed: (seconds) => {
              if (requestSequenceRef.current === requestSequence) {
                setCurrentTime(seconds);
              }
            },
            onDuration: (seconds) => {
              if (requestSequenceRef.current === requestSequence) {
                setDuration(seconds);
              }
            },
          });
          if (requestSequenceRef.current !== requestSequence) {
            playback.stop();
            return;
          }
          requestRef.current = null;
          livePlaybackRef.current = playback;
          audioRef.current = playback.audio;
          setCurrentModel(playback.model);
          void playback.done.then(() => {
            if (requestSequenceRef.current !== requestSequence) {
              return;
            }
            livePlaybackRef.current = null;
            audioRef.current = null;
            setStatus("idle");
            setStatusMessage(autoSpeakEnabled ? "The broadcast is completed, waiting for the next reply" : "Report completed");
          }).catch((error: unknown) => {
            if (requestSequenceRef.current !== requestSequence) {
              return;
            }
            livePlaybackRef.current = null;
            audioRef.current = null;
            setStatus("error");
            setStatusMessage(error instanceof Error ? error.message : "Codex Live broadcast failed");
          });
          return;
        }
        const response = await synthesizeSpeech(
          speechText,
          { voice: selectedVoice, speechRate },
          controller.signal
        );
        if (requestSequenceRef.current !== requestSequence) {
          return;
        }
        requestRef.current = null;
        const audioUrl = URL.createObjectURL(response.audio);
        const audio = new Audio(audioUrl);
        audioUrlRef.current = audioUrl;
        audioRef.current = audio;
        setCurrentModel(response.model || speechOptions.model);
        const modelLabel = [response.model, response.voice].filter(Boolean).join(" · ");
        const updateDuration = () => {
          if (Number.isFinite(audio.duration) && audio.duration > 0) {
            setDuration(audio.duration);
          }
        };
        audio.onloadedmetadata = updateDuration;
        audio.ondurationchange = updateDuration;
        audio.ontimeupdate = () => {
          setCurrentTime(audio.currentTime);
          updateDuration();
        };
        audio.onplay = () => {
          setStatus("playing");
          setStatusMessage(modelLabel ? `Speaking · ${modelLabel}` : "Speaking now");
        };
        audio.onended = () => {
          const finalDuration = Number.isFinite(audio.duration) ? audio.duration : 0;
          if (finalDuration > 0) {
            setDuration(finalDuration);
            setCurrentTime(finalDuration);
          }
          releaseAudio();
          setStatus("idle");
          setStatusMessage(autoSpeakEnabled ? "The broadcast is completed, waiting for the next reply" : "Report completed");
        };
        audio.onerror = () => {
          releaseAudio();
          setStatus("error");
          setStatusMessage("The browser did not successfully play this audio");
        };
        await audio.play();
      } catch (error) {
        if (controller.signal.aborted || requestSequenceRef.current !== requestSequence) {
          return;
        }
        requestRef.current = null;
        releaseAudio();
        setStatus("error");
        setStatusMessage(error instanceof Error ? error.message : "Voice broadcast failed");
      }
    },
    [
      api,
      autoSpeakEnabled,
      speechAvailable,
      lessonId,
      releaseAudio,
      selectedVoice,
      speechOptions.delivery,
      speechOptions.model,
      speechRate,
    ]
  );

  const replayCurrentSpeech = useCallback(() => {
    if (currentSpeechText) {
      void speakMessage(currentSpeechText);
    }
  }, [currentSpeechText, speakMessage]);

  const pauseSpeech = useCallback(() => {
    const audio = audioRef.current;
    if (!audio || audio.paused) {
      return;
    }
    audio.pause();
    setCurrentTime(audio.currentTime);
    setStatus("paused");
    setStatusMessage("The broadcast has been paused. You can continue from the current position.");
  }, []);

  const resumeSpeech = useCallback(async () => {
    const audio = audioRef.current;
    if (!audio || !audio.paused) {
      return;
    }
    try {
      await audio.play();
    } catch (error) {
      releaseAudio();
      setStatus("error");
      setStatusMessage(error instanceof Error ? error.message : "The browser failed to continue playing the audio");
    }
  }, [releaseAudio]);

  const seekSpeech = useCallback((nextTime: number) => {
    const audio = audioRef.current;
    if (!audio || !Number.isFinite(nextTime)) {
      return;
    }
    const boundedTime = Math.max(0, Math.min(audio.duration || nextTime, nextTime));
    audio.currentTime = boundedTime;
    setCurrentTime(boundedTime);
  }, []);

  const selectVoice = useCallback(
    (voice: string) => {
      if (!speechOptions.voices.some((option) => option.id === voice)) {
        return;
      }
      setSelectedVoiceState(voice);
      try {
        window.localStorage.setItem(SPEECH_VOICE_STORAGE_KEY, voice);
      } catch {
        // The in-memory selection still works when storage is unavailable.
      }
    },
    [speechOptions.voices]
  );

  const selectSpeechRate = useCallback(
    (nextRate: number) => {
      const boundedRate = Math.max(
        speechOptions.minimum_speech_rate,
        Math.min(speechOptions.maximum_speech_rate, Math.round(nextRate))
      );
      setSpeechRateState(boundedRate);
      try {
        window.localStorage.setItem(SPEECH_RATE_STORAGE_KEY, String(boundedRate));
      } catch {
        // The in-memory selection still works when storage is unavailable.
      }
    },
    [speechOptions.maximum_speech_rate, speechOptions.minimum_speech_rate]
  );

  const toggleAutoSpeak = useCallback(() => {
    const nextEnabled = !autoSpeakEnabled;
    try {
      window.localStorage.setItem(AUTO_SPEECH_STORAGE_KEY, nextEnabled ? "true" : "false");
    } finally {
      window.dispatchEvent(new Event(AUTO_SPEECH_CHANGE_EVENT));
    }
    if (nextEnabled) {
      setStatus("idle");
      setStatusMessage("Waiting for new AI reply");
      return;
    }
    requestSequenceRef.current += 1;
    requestRef.current?.abort();
    requestRef.current = null;
    releaseAudio();
    setCurrentTime(0);
    setDuration(0);
    setStatus("idle");
    setStatusMessage("Auto-announcement is turned off");
  }, [autoSpeakEnabled, releaseAudio]);

  useEffect(() => {
    const controller = new AbortController();
    if (!speechAvailable) return () => controller.abort();
    void getSpeechOptions(controller.signal)
      .then((options) => {
        setSpeechOptions({ ...options, available: true });
        setCurrentModel(options.model);
        let storedVoice: string | null = null;
        let storedRate: number | null = null;
        try {
          storedVoice = window.localStorage.getItem(SPEECH_VOICE_STORAGE_KEY);
          const rawRate = window.localStorage.getItem(SPEECH_RATE_STORAGE_KEY);
          storedRate = rawRate === null ? null : Number(rawRate);
        } catch {
          // Use provider defaults when storage is unavailable.
        }
        const nextVoice = options.voices.some((voice) => voice.id === storedVoice)
          ? storedVoice!
          : options.default_voice;
        const nextRate = Number.isFinite(storedRate)
          ? Math.max(
              options.minimum_speech_rate,
              Math.min(options.maximum_speech_rate, Math.round(storedRate!))
            )
          : options.default_speech_rate;
        setSelectedVoiceState(nextVoice);
        setSpeechRateState(nextRate);
      })
      .catch(() => {
        // The default voice remains available if option discovery fails.
      });
    return () => controller.abort();
  }, [speechAvailable]);

  useEffect(() => {
    const latestId = latestAssistantMessage?.id ?? null;
    if (trackedLessonIdRef.current !== lessonId) {
      requestSequenceRef.current += 1;
      requestRef.current?.abort();
      requestRef.current = null;
      releaseAudio();
      setCurrentSpeechText("");
      setCurrentTime(0);
      setDuration(0);
      trackedLessonIdRef.current = lessonId;
      latestSeenAssistantIdRef.current = latestId;
      setStatus("idle");
      setStatusMessage(autoSpeakEnabled ? "Waiting for new AI reply" : "Auto-announcement is turned off");
      return;
    }
    if (!latestAssistantMessage || latestSeenAssistantIdRef.current === latestId) {
      return;
    }
    latestSeenAssistantIdRef.current = latestId;
    if (autoSpeakEnabled) {
      const timeoutId = window.setTimeout(() => {
        void speakMessage(latestAssistantMessage.content);
      }, 0);
      return () => window.clearTimeout(timeoutId);
    }
  }, [autoSpeakEnabled, latestAssistantMessage, lessonId, releaseAudio, speakMessage]);

  useEffect(() => {
    return () => {
      requestSequenceRef.current += 1;
      requestRef.current?.abort();
      releaseAudio();
    };
  }, [releaseAudio]);

  return {
    autoSpeakEnabled,
    isSpeechLoading: status === "loading",
    isSpeechPlaying: status === "playing",
    isSpeechPaused: status === "paused",
    speechStatusText: statusMessage,
    speechOptions,
    selectedVoice,
    speechRate,
    currentModel,
    currentSpeechText,
    currentTime,
    duration,
    canSeekSpeech:
      speechOptions.supports_seek &&
      (status === "playing" || status === "paused") &&
      duration > 0,
    canReplaySpeech: Boolean(currentSpeechText) && (status === "idle" || status === "error"),
    speakMessage,
    replayCurrentSpeech,
    pauseSpeech,
    resumeSpeech,
    seekSpeech,
    selectVoice,
    selectSpeechRate,
    stopSpeech,
    toggleAutoSpeak,
  };
}
