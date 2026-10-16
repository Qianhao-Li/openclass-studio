import clsx from "clsx";
import { Pause, Play, RotateCcw, Volume2, VolumeX, X } from "lucide-react";
import type { CSSProperties } from "react";

import type { SpeechOptionsResponse } from "../../lib/speech-api";

import styles from "./voice-control-panel.module.css";

type VoiceControlPanelProps = {
  autoEnabled: boolean;
  isLoading: boolean;
  isPlaying: boolean;
  isPaused: boolean;
  statusText: string;
  model: string;
  currentText: string;
  currentTime: number;
  duration: number;
  canSeek: boolean;
  canReplay: boolean;
  options: SpeechOptionsResponse;
  selectedVoice: string;
  speechRate: number;
  onAutoToggle: () => void;
  onCancel: () => void;
  onPause: () => void;
  onResume: () => void;
  onReplay: () => void;
  onSeek: (time: number) => void;
  onVoiceChange: (voice: string) => void;
  onSpeechRateChange: (rate: number) => void;
};

const SPEECH_RATE_STEP = 5;
const SPEECH_RATE_SLIDER_MIN = 0;
const SPEECH_RATE_SLIDER_MIDPOINT = 50;
const SPEECH_RATE_SLIDER_MAX = 100;

function formatPlaybackTime(value: number) {
  if (!Number.isFinite(value) || value <= 0) {
    return "0:00";
  }
  const totalSeconds = Math.floor(value);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = String(totalSeconds % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function formatSpeechRate(value: number) {
  const multiplier = 1 + value / 100;
  return `${multiplier.toFixed(2).replace(/\.?0+$/, "")}×`;
}

function clamp(value: number, minimum: number, maximum: number) {
  return Math.max(minimum, Math.min(maximum, value));
}

function getSpeechRateSliderPosition(value: number, minimum: number, maximum: number) {
  if (maximum <= minimum) {
    return SPEECH_RATE_SLIDER_MIN;
  }

  const boundedValue = clamp(value, minimum, maximum);
  if (minimum < 0 && maximum > 0) {
    if (boundedValue <= 0) {
      return ((boundedValue - minimum) / -minimum) * SPEECH_RATE_SLIDER_MIDPOINT;
    }
    return (
      SPEECH_RATE_SLIDER_MIDPOINT +
      (boundedValue / maximum) * (SPEECH_RATE_SLIDER_MAX - SPEECH_RATE_SLIDER_MIDPOINT)
    );
  }

  return ((boundedValue - minimum) / (maximum - minimum)) * SPEECH_RATE_SLIDER_MAX;
}

function getSpeechRateFromSliderPosition(position: number, minimum: number, maximum: number) {
  if (maximum <= minimum) {
    return minimum;
  }

  const boundedPosition = clamp(
    position,
    SPEECH_RATE_SLIDER_MIN,
    SPEECH_RATE_SLIDER_MAX
  );
  let speechRate: number;
  if (minimum < 0 && maximum > 0) {
    speechRate =
      boundedPosition <= SPEECH_RATE_SLIDER_MIDPOINT
        ? minimum + (boundedPosition / SPEECH_RATE_SLIDER_MIDPOINT) * -minimum
        : ((boundedPosition - SPEECH_RATE_SLIDER_MIDPOINT) /
            (SPEECH_RATE_SLIDER_MAX - SPEECH_RATE_SLIDER_MIDPOINT)) *
          maximum;
  } else {
    speechRate = minimum + (boundedPosition / SPEECH_RATE_SLIDER_MAX) * (maximum - minimum);
  }

  const snappedRate =
    Math.sign(speechRate) * Math.round(Math.abs(speechRate) / SPEECH_RATE_STEP) * SPEECH_RATE_STEP;
  return clamp(snappedRate, minimum, maximum);
}

export function VoiceControlPanel({
  autoEnabled,
  isLoading,
  isPlaying,
  isPaused,
  statusText,
  model,
  currentText,
  currentTime,
  duration,
  canSeek,
  canReplay,
  options,
  selectedVoice,
  speechRate,
  onAutoToggle,
  onCancel,
  onPause,
  onResume,
  onReplay,
  onSeek,
  onVoiceChange,
  onSpeechRateChange,
}: VoiceControlPanelProps) {
  const speechRateSliderPosition = getSpeechRateSliderPosition(
    speechRate,
    options.minimum_speech_rate,
    options.maximum_speech_rate
  );

  return (
    <div className="space-y-4">
      <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              {autoEnabled ? (
                <Volume2 className="h-4 w-4 shrink-0 text-gray-800" />
              ) : (
                <VolumeX className="h-4 w-4 shrink-0 text-gray-400" />
              )}
              <p className="text-sm font-semibold text-gray-900">AI reply automatic broadcast</p>
            </div>
            <p className="mt-2 text-xs leading-5 text-gray-500">

              AI automatically uses Codex Live to generate draggable speech after generating a new reply in the chat box.
            </p>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={autoEnabled}
            aria-label="AI reply automatic broadcast"
            onClick={onAutoToggle}
            className={clsx(
              "relative mt-0.5 h-7 w-12 shrink-0 rounded-full border transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-black focus-visible:ring-offset-2",
              autoEnabled ? "border-black bg-black" : "border-gray-300 bg-gray-200"
            )}
          >
            <span
              className={clsx(
                "pointer-events-none absolute left-1 top-1 h-5 w-5 rounded-full bg-white shadow-sm ring-1 ring-black/5 transition-transform duration-200 dark:bg-white",
                autoEnabled ? "translate-x-5" : "translate-x-0"
              )}
            />
          </button>
        </div>
      </section>

      <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <div className="flex items-center justify-between gap-3">
          <p className="text-[11px] font-bold uppercase tracking-wider text-gray-500">Currently playing</p>
          <span className="rounded-full bg-gray-100 px-2 py-1 text-[10px] font-medium text-gray-600">
            {model || options.model}
            {options.delivery === "realtime_audio" ? " · LIVE" : ""}
          </span>
        </div>

        <p className="mt-3 max-h-14 overflow-hidden text-xs leading-5 text-gray-700">
          {currentText || "After a new AI reply is generated, the content being broadcast will be displayed here."}
        </p>

        {options.supports_seek ? <div className="mt-4">
          <input
            type="range"
            min={0}
            max={duration > 0 ? duration : 1}
            step={0.1}
            value={duration > 0 ? Math.min(currentTime, duration) : 0}
            disabled={!canSeek}
            onChange={(event) => onSeek(Number(event.target.value))}
            aria-label="Playback progress"
            className="h-1.5 w-full cursor-pointer accent-black disabled:cursor-not-allowed disabled:opacity-40"
          />
          <div className="mt-1.5 flex items-center justify-between text-[10px] tabular-nums text-gray-400">
            <span>{formatPlaybackTime(currentTime)}</span>
            <span>{formatPlaybackTime(duration)}</span>
          </div>
        </div> : (
          <div className="mt-4 flex items-center gap-3 text-[10px] tabular-nums text-gray-400">
            <span className={clsx("h-1.5 flex-1 rounded-full bg-gray-100", isPlaying && "animate-pulse bg-black/70")} />
            <span>{formatPlaybackTime(currentTime)} · LIVE</span>
          </div>
        )}

        <div className="mt-3 flex items-center justify-between gap-3 border-t border-gray-100 pt-3">
          <p className="min-w-0 flex-1 text-xs leading-5 text-gray-500" title={statusText}>
            {statusText}
          </p>
          {isLoading ? (
            <button
              type="button"
              onClick={onCancel}
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-2.5 text-xs font-medium text-gray-700 transition hover:bg-gray-50"
              aria-label="Cancel speech generation"
            >
              <X className="h-3 w-3" />

              Cancel
            </button>
          ) : isPlaying && !options.supports_seek ? (
            <button
              type="button"
              onClick={onCancel}
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-2.5 text-xs font-medium text-gray-700 transition hover:bg-gray-50"
              aria-label="Stop broadcasting"
            >
              <X className="h-3 w-3" />

              stop
            </button>
          ) : isPlaying ? (
            <button
              type="button"
              onClick={onPause}
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-2.5 text-xs font-medium text-gray-700 transition hover:bg-gray-50"
              aria-label="Pause broadcast"
            >
              <Pause className="h-3 w-3 fill-current" />

              pause
            </button>
          ) : isPaused ? (
            <button
              type="button"
              onClick={onResume}
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-2.5 text-xs font-medium text-gray-700 transition hover:bg-gray-50"
              aria-label="Continue broadcasting"
            >
              <Play className="h-3 w-3 fill-current" />

              continue
            </button>
          ) : canReplay ? (
            <button
              type="button"
              onClick={onReplay}
              className="inline-flex h-8 shrink-0 items-center gap-1.5 rounded-lg border border-gray-200 bg-white px-2.5 text-xs font-medium text-gray-700 transition hover:bg-gray-50"
            >
              <RotateCcw className="h-3 w-3" />

              Replay
            </button>
          ) : null}
        </div>
      </section>

      <section className="rounded-xl border border-gray-200 bg-white p-4 shadow-sm">
        <p className="text-[11px] font-bold uppercase tracking-wider text-gray-500">sound settings</p>

        <label className="mt-4 block text-xs font-medium text-gray-700" htmlFor="speech-voice-select">

          timbre
        </label>
        <select
          id="speech-voice-select"
          value={selectedVoice}
          onChange={(event) => onVoiceChange(event.target.value)}
          className="mt-2 h-10 w-full rounded-lg border border-gray-200 bg-white px-3 text-sm text-gray-800 outline-none transition focus:border-gray-400 focus:ring-2 focus:ring-gray-100"
        >
          {options.voices.map((voice) => (
            <option key={voice.id} value={voice.id}>
              {voice.label} · {voice.description}
            </option>
          ))}
        </select>

        {options.supports_speech_rate ? <><div className="mt-5 flex items-center justify-between gap-3">
          <label className="text-xs font-medium text-gray-700" htmlFor="speech-rate-range">

            speaking speed
          </label>
          <span className="rounded-md bg-gray-100 px-2 py-1 text-xs font-semibold tabular-nums text-gray-700">
            {formatSpeechRate(speechRate)}
          </span>
        </div>
        <input
          id="speech-rate-range"
          type="range"
          min={SPEECH_RATE_SLIDER_MIN}
          max={SPEECH_RATE_SLIDER_MAX}
          step={0.1}
          value={speechRateSliderPosition}
          aria-valuetext={formatSpeechRate(speechRate)}
          onChange={(event) =>
            onSpeechRateChange(
              getSpeechRateFromSliderPosition(
                Number(event.target.value),
                options.minimum_speech_rate,
                options.maximum_speech_rate
              )
            )
          }
          onKeyDown={(event) => {
            const direction =
              event.key === "ArrowLeft" || event.key === "ArrowDown"
                ? -1
                : event.key === "ArrowRight" || event.key === "ArrowUp"
                  ? 1
                  : 0;
            if (direction !== 0) {
              event.preventDefault();
              onSpeechRateChange(
                clamp(
                  speechRate + direction * SPEECH_RATE_STEP,
                  options.minimum_speech_rate,
                  options.maximum_speech_rate
                )
              );
            }
          }}
          className={clsx(styles.rateRange, "mt-3 h-5 w-full")}
          style={{
            "--speech-rate-progress": `${speechRateSliderPosition}%`,
          } as CSSProperties}
        />
        <div className="mt-1.5 flex justify-between text-[10px] text-gray-400">
          <span>0.5×</span>
          <span>1.0×</span>
          <span>2.0×</span>
        </div>
        <p className="mt-3 text-[11px] leading-5 text-gray-400">

          The new settings will take effect the next time the auto-announcement or replay occurs.
        </p>
        </> : (
          <p className="mt-4 text-[11px] leading-5 text-gray-400">

            Codex Live will naturally control the speaking speed according to the content; the new tone will take effect during the next broadcast.
          </p>
        )}
      </section>
    </div>
  );
}
