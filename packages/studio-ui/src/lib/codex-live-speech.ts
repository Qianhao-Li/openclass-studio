import { getApiWebSocketUrl, type api } from "./api";
import { connectLiveSpeech } from "./speech-api";

type CodexLiveSpeechOptions = {
  client?: Pick<typeof api, "request" | "webSocketUrl">;
  lessonId: string;
  text: string;
  voice: string;
  signal: AbortSignal;
  onPlaying: (audio: HTMLAudioElement, model: string, voice: string) => void;
  onStatus: (message: string) => void;
  onElapsed: (seconds: number) => void;
  onDuration: (seconds: number) => void;
};

export type CodexLiveSpeechPlayback = {
  audio: HTMLAudioElement;
  model: string;
  voice: string;
  done: Promise<void>;
  stop: () => void;
};

type BridgeEvent = {
  type?: string;
  role?: string;
  message?: string;
};

type RealtimeEvent = {
  type?: string;
  error?: { message?: string };
};

const LIVE_CONNECT_TIMEOUT_MS = 20_000;
const LIVE_RESPONSE_TIMEOUT_MS = 120_000;
const AUDIO_DRAIN_DELAY_MS = 1_500;
const AUDIO_METADATA_TIMEOUT_MS = 10_000;

function waitForPeerConnection(
  peerConnection: RTCPeerConnection,
  signal: AbortSignal
): Promise<void> {
  if (peerConnection.connectionState === "connected") {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(
      () => finish(new Error("Codex Live audio connection timeout")),
      LIVE_CONNECT_TIMEOUT_MS
    );
    const onAbort = () => finish(new DOMException("Aborted", "AbortError"));
    const onStateChange = () => {
      if (peerConnection.connectionState === "connected") {
        finish();
      } else if (["failed", "closed", "disconnected"].includes(peerConnection.connectionState)) {
        finish(new Error("Codex Live audio connection disconnected"));
      }
    };
    const finish = (error?: Error) => {
      window.clearTimeout(timeoutId);
      signal.removeEventListener("abort", onAbort);
      peerConnection.removeEventListener("connectionstatechange", onStateChange);
      if (error) {
        reject(error);
      } else {
        resolve();
      }
    };
    signal.addEventListener("abort", onAbort, { once: true });
    peerConnection.addEventListener("connectionstatechange", onStateChange);
    onStateChange();
  });
}

function preferredAudioMimeType() {
  if (typeof MediaRecorder === "undefined") {
    return null;
  }
  return (
    [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/mp4;codecs=mp4a.40.2",
      "audio/mp4",
    ].find((mimeType) => MediaRecorder.isTypeSupported(mimeType)) ?? ""
  );
}

function waitForSeekableMetadata(
  audio: HTMLAudioElement,
  signal: AbortSignal
): Promise<number> {
  return new Promise((resolve, reject) => {
    let probingDuration = false;
    const timeoutId = window.setTimeout(
      () => finish(new Error("The browser cannot read the Codex Live audio duration")),
      AUDIO_METADATA_TIMEOUT_MS
    );
    const onAbort = () => finish(new DOMException("Aborted", "AbortError"));
    const readDuration = () => {
      if (Number.isFinite(audio.duration) && audio.duration > 0) {
        const duration = audio.duration;
        audio.currentTime = 0;
        finish(undefined, duration);
        return;
      }
      if (!probingDuration && audio.readyState >= HTMLMediaElement.HAVE_METADATA) {
        probingDuration = true;
        audio.currentTime = Number.MAX_SAFE_INTEGER;
      }
    };
    const finish = (error?: Error, duration?: number) => {
      window.clearTimeout(timeoutId);
      signal.removeEventListener("abort", onAbort);
      audio.removeEventListener("loadedmetadata", readDuration);
      audio.removeEventListener("durationchange", readDuration);
      audio.removeEventListener("timeupdate", readDuration);
      if (error) {
        reject(error);
      } else {
        resolve(duration ?? 0);
      }
    };
    signal.addEventListener("abort", onAbort, { once: true });
    audio.addEventListener("loadedmetadata", readDuration);
    audio.addEventListener("durationchange", readDuration);
    audio.addEventListener("timeupdate", readDuration);
    readDuration();
  });
}

export async function startCodexLiveSpeech({
  client,
  lessonId,
  text,
  voice,
  signal,
  onPlaying,
  onStatus,
  onElapsed,
  onDuration,
}: CodexLiveSpeechOptions): Promise<CodexLiveSpeechPlayback> {
  const mimeType = preferredAudioMimeType();
  if (mimeType === null) {
    throw new Error("The current browser does not support draggable Codex Live audio");
  }

  const peerConnection = new RTCPeerConnection();
  const dataChannel = peerConnection.createDataChannel("oai-events");
  const audio = new Audio();
  audio.preload = "metadata";
  peerConnection.addTransceiver("audio", { direction: "recvonly" });

  let bridgeSocket: WebSocket | null = null;
  let mediaRecorder: MediaRecorder | null = null;
  let liveModel = "gpt-live-1-codex";
  let liveVoice = voice;
  let audioUrl: string | null = null;
  let completionTimer: number | null = null;
  let drainTimer: number | null = null;
  let remoteTrackTimer: number | null = null;
  let stopped = false;
  let transportClosing = false;
  let captureFinalizing = false;
  const recordedChunks: Blob[] = [];
  let resolveDone!: () => void;
  let rejectDone!: (error: Error) => void;
  let resolveRemoteTrack!: () => void;
  let rejectRemoteTrack!: (error: Error) => void;

  const done = new Promise<void>((resolve, reject) => {
    resolveDone = resolve;
    rejectDone = reject;
  });
  const remoteTrackReady = new Promise<void>((resolve, reject) => {
    resolveRemoteTrack = resolve;
    rejectRemoteTrack = reject;
  });
  remoteTrackTimer = window.setTimeout(
    () => rejectRemoteTrack(new Error("Codex Live does not create remote audio track")),
    LIVE_CONNECT_TIMEOUT_MS
  );

  const clearTimers = () => {
    if (completionTimer !== null) {
      window.clearTimeout(completionTimer);
      completionTimer = null;
    }
    if (drainTimer !== null) {
      window.clearTimeout(drainTimer);
      drainTimer = null;
    }
    if (remoteTrackTimer !== null) {
      window.clearTimeout(remoteTrackTimer);
      remoteTrackTimer = null;
    }
  };

  const closeTransport = () => {
    if (transportClosing) {
      return;
    }
    transportClosing = true;
    bridgeSocket?.close();
    dataChannel.close();
    peerConnection.close();
  };

  const releaseAudio = () => {
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    if (audioUrl) {
      URL.revokeObjectURL(audioUrl);
      audioUrl = null;
    }
  };

  const stop = () => {
    if (stopped) {
      return;
    }
    stopped = true;
    clearTimers();
    if (mediaRecorder?.state === "recording") {
      mediaRecorder.stop();
    }
    closeTransport();
    releaseAudio();
    resolveDone();
  };

  const fail = (error: Error) => {
    if (stopped) {
      return;
    }
    stopped = true;
    clearTimers();
    if (mediaRecorder?.state === "recording") {
      mediaRecorder.stop();
    }
    closeTransport();
    releaseAudio();
    rejectRemoteTrack(error);
    rejectDone(error);
  };

  const playRecordedAudio = async () => {
    if (stopped) {
      return;
    }
    if (!recordedChunks.length) {
      fail(new Error("Codex Live does not return playable audio"));
      return;
    }

    closeTransport();
    const recordedAudio = new Blob(
      recordedChunks,
      mimeType ? { type: mimeType } : undefined
    );
    if (!recordedAudio.size) {
      fail(new Error("Codex Live returned empty audio"));
      return;
    }

    audioUrl = URL.createObjectURL(recordedAudio);
    audio.src = audioUrl;
    audio.onerror = () => fail(new Error("The browser did not successfully play Codex Live audio"));

    try {
      const duration = await waitForSeekableMetadata(audio, signal);
      if (stopped) {
        return;
      }
      onDuration(duration);
      onElapsed(0);
      audio.ontimeupdate = () => onElapsed(audio.currentTime);
      audio.onplay = () => onPlaying(audio, liveModel, liveVoice);
      audio.onended = stop;
      onStatus("Codex Live audio has been generated and is being broadcast");
      await audio.play();
    } catch (error) {
      fail(error instanceof Error ? error : new Error("The browser did not successfully play Codex Live audio"));
    }
  };

  const finishCaptureAfterDrain = () => {
    if (stopped || captureFinalizing) {
      return;
    }
    captureFinalizing = true;
    if (completionTimer !== null) {
      window.clearTimeout(completionTimer);
      completionTimer = null;
    }
    onStatus("Codex Live is organizing draggable audio…");
    drainTimer = window.setTimeout(() => {
      drainTimer = null;
      if (!mediaRecorder || mediaRecorder.state !== "recording") {
        fail(new Error("Codex Live does not create audio recording track"));
        return;
      }
      mediaRecorder.stop();
    }, AUDIO_DRAIN_DELAY_MS);
  };

  const abort = () => fail(new DOMException("Aborted", "AbortError"));
  signal.addEventListener("abort", abort, { once: true });
  done.finally(() => signal.removeEventListener("abort", abort)).catch(() => undefined);
  remoteTrackReady.catch(() => undefined);

  peerConnection.ontrack = (event) => {
    if (mediaRecorder || stopped) {
      return;
    }
    try {
      const [remoteStream] = event.streams;
      const stream = remoteStream ?? new MediaStream([event.track]);
      mediaRecorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
      mediaRecorder.ondataavailable = (dataEvent) => {
        if (dataEvent.data.size > 0) {
          recordedChunks.push(dataEvent.data);
        }
      };
      mediaRecorder.onerror = () => fail(new Error("Browser fails to record Codex Live audio"));
      mediaRecorder.onstop = () => {
        if (!stopped) {
          void playRecordedAudio();
        }
      };
      mediaRecorder.start(250);
      if (remoteTrackTimer !== null) {
        window.clearTimeout(remoteTrackTimer);
        remoteTrackTimer = null;
      }
      resolveRemoteTrack();
    } catch (error) {
      const normalizedError =
        error instanceof Error ? error : new Error("The browser cannot record Codex Live audio");
      rejectRemoteTrack(normalizedError);
      fail(normalizedError);
    }
  };

  dataChannel.onmessage = (event) => {
    try {
      const payload = JSON.parse(String(event.data)) as RealtimeEvent;
      if (payload.type === "response.created") {
        onStatus("Codex Live is generating speech…");
      } else if (
        payload.type === "response.done" ||
        payload.type === "response.audio.done" ||
        payload.type === "response.output_audio.done"
      ) {
        finishCaptureAfterDrain();
      } else if (payload.type === "error") {
        fail(new Error(payload.error?.message || "Codex Live broadcast failed"));
      }
    } catch {
      // Ignore unrelated transport events.
    }
  };

  try {
    const offer = await peerConnection.createOffer();
    await peerConnection.setLocalDescription(offer);
    const clientSessionId = `speech_${crypto.randomUUID()}`;
    const response = await connectLiveSpeech(
      lessonId,
      {
        offerSdp: offer.sdp ?? "",
        clientSessionId,
        voice,
      },
      signal,
      client?.request
    );
    if (!response.delegation_websocket_url) {
      throw new Error("Codex Live response to missing broadcast control channel");
    }
    liveModel = response.model;
    liveVoice = response.voice;
    await peerConnection.setRemoteDescription({ type: "answer", sdp: response.answer_sdp });
    bridgeSocket = new WebSocket((client?.webSocketUrl ?? getApiWebSocketUrl)(response.delegation_websocket_url));

    const bridgeReady = new Promise<void>((resolve, reject) => {
      const timeoutId = window.setTimeout(
        () => reject(new Error("Codex Live reports control channel connection timeout")),
        LIVE_CONNECT_TIMEOUT_MS
      );
      const finishReady = () => {
        window.clearTimeout(timeoutId);
        resolve();
      };
      bridgeSocket!.onerror = () => reject(new Error("Codex Live reports control channel connection failure"));
      bridgeSocket!.onclose = () => {
        if (!stopped && !transportClosing) {
          fail(new Error("Codex Live broadcast control channel has been disconnected"));
        }
      };
      bridgeSocket!.onmessage = (event) => {
        try {
          const payload = JSON.parse(String(event.data)) as BridgeEvent;
          if (payload.type === "codex_live.ready") {
            finishReady();
          } else if (payload.type === "codex_live.announcement.accepted") {
            onStatus("Codex Live has received the broadcast content");
          } else if (
            payload.type === "codex_live.announcement.error" ||
            payload.type === "codex_live.error"
          ) {
            fail(new Error(payload.message || "Codex Live broadcast failed"));
          } else if (
            payload.type === "codex_live.transcript.done" &&
            payload.role === "assistant"
          ) {
            finishCaptureAfterDrain();
          }
        } catch {
          // Ignore unrelated bridge events.
        }
      };
    });

    await Promise.all([
      bridgeReady,
      waitForPeerConnection(peerConnection, signal),
      remoteTrackReady,
    ]);
    if (bridgeSocket.readyState !== WebSocket.OPEN) {
      throw new Error("Codex Live broadcast control channel is not ready yet");
    }
    bridgeSocket.send(JSON.stringify({ type: "announcement.play", text }));
    completionTimer = window.setTimeout(
      () => fail(new Error("Codex Live broadcast wait timeout")),
      LIVE_RESPONSE_TIMEOUT_MS
    );
    onStatus("Codex Live is generating draggable audio…");
    return {
      audio,
      model: response.model,
      voice: response.voice,
      done,
      stop,
    };
  } catch (error) {
    const normalizedError =
      error instanceof Error ? error : new Error("Codex Live reports connection failure");
    fail(normalizedError);
    throw normalizedError;
  }
}
