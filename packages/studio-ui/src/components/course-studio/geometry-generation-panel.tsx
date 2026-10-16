"use client";

import { Box, LoaderCircle, Sparkles, Trash2 } from "lucide-react";
import { useRef } from "react";

import { ChatAttachmentChips, ChatAttachmentMenu } from "./chat-attachment-menu";
import { GeometrySceneViewer } from "./geometry-scene-viewer";
import type { ChatAttachmentRef, SelectionRef } from "../../types";
import type { GeometryScene } from "../../types/geometry";

type GeometryGenerationPanelProps = {
  packageId: string;
  selection: SelectionRef | null;
  instructions: string;
  attachments: ChatAttachmentRef[];
  scene: GeometryScene | null;
  error: string;
  isGenerating: boolean;
  onInstructionsChange: (value: string) => void;
  onAttachmentsChange: (attachments: ChatAttachmentRef[]) => void;
  onAttachmentError: (message: string) => void;
  onGenerate: () => void | Promise<void>;
  onClear: () => void;
};

export function GeometryGenerationPanel({
  packageId,
  selection,
  instructions,
  attachments,
  scene,
  error,
  isGenerating,
  onInstructionsChange,
  onAttachmentsChange,
  onAttachmentError,
  onGenerate,
  onClear,
}: GeometryGenerationPanelProps) {
  const attachmentBoundaryRef = useRef<HTMLDivElement | null>(null);
  const attachmentsReady = attachments.every(
    (attachment) => attachment.kind === "image" || attachment.status === "ready"
  );

  return (
    <div className="space-y-4" data-geometry-generation-panel>
      <div className="rounded-2xl border border-slate-200 bg-gradient-to-br from-slate-950 to-slate-800 p-4 text-white">
        <div className="flex items-center gap-2">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-white/10">
            <Box className="h-4 w-4" />
          </span>
          <div>
            <h3 className="text-sm font-semibold">Geometry generation</h3>
            <p className="mt-0.5 text-[11px] text-slate-300">Reference the board content to generate a rotatable and scalable scene</p>
          </div>
        </div>
      </div>

      {selection ? (
        <section className="rounded-2xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex items-center justify-between gap-3">
            <p className="text-[10px] font-bold uppercase tracking-[0.18em] text-slate-400">Quoted from the board</p>
            <button type="button" onClick={onClear} className="rounded-lg p-1.5 text-slate-400 transition hover:bg-slate-100 hover:text-slate-700" aria-label="Clear drawing references">
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          </div>
          <p className="mt-3 max-h-36 overflow-y-auto whitespace-pre-wrap break-words rounded-xl bg-slate-50 p-3 text-[12px] leading-6 text-slate-700">
            {selection.excerpt}
          </p>
          <label className="mt-4 block text-[11px] font-semibold text-slate-600" htmlFor="geometry-guidance">

            Additional requirements (optional)
          </label>
          <textarea
            id="geometry-guidance"
            value={instructions}
            onChange={(event) => onInstructionsChange(event.target.value)}
            rows={3}
            maxLength={2000}
            placeholder="For example: highlight parallel relationships, or use a stereoscopic perspective"
            className="mt-2 w-full resize-y rounded-xl border border-slate-200 bg-white px-3 py-2 text-[12px] leading-5 text-slate-800 outline-none transition placeholder:text-slate-300 focus:border-slate-400"
          />
          <div ref={attachmentBoundaryRef} className="mt-3 rounded-xl border border-slate-200 bg-slate-50/70 py-1.5">
            <ChatAttachmentChips
              attachments={attachments}
              disabled={isGenerating}
              onRemove={(sourceId) =>
                onAttachmentsChange(
                  attachments.filter((attachment) => attachment.source_ingestion_id !== sourceId)
                )
              }
            />
            <div className="flex min-h-9 items-center gap-1.5 px-2">
              <ChatAttachmentMenu
                packageId={packageId}
                attachments={attachments}
                disabled={isGenerating}
                menuAboveRef={attachmentBoundaryRef}
                limitLabel="Generate each time"
                testIdPrefix="geometry"
                triggerText="Add photos and files"
                triggerHint="Upload from computer"
                onChange={onAttachmentsChange}
                onError={onAttachmentError}
              />
            </div>
          </div>
          <button
            type="button"
            disabled={isGenerating || !attachmentsReady}
            onClick={() => void onGenerate()}
            className="mt-3 inline-flex h-10 w-full items-center justify-center gap-2 rounded-xl bg-slate-950 px-4 text-[12px] font-semibold text-white transition hover:bg-black disabled:cursor-not-allowed disabled:opacity-55"
          >
            {isGenerating ? <LoaderCircle className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
            {isGenerating ? "Building graph..." : scene ? "Regenerate" : "Generate graphics"}
          </button>
          {!attachmentsReady ? (
            <p className="mt-2 text-[10px] leading-4 text-amber-700">The file is being parsed and the graph will be generated once complete.</p>
          ) : null}
        </section>
      ) : (
        <section className="rounded-2xl border border-dashed border-slate-300 bg-white px-5 py-8 text-center">
          <Box className="mx-auto h-7 w-7 text-slate-300" />
          <h3 className="mt-3 text-sm font-semibold text-slate-800">First quote the content from the board</h3>
          <p className="mt-2 text-[12px] leading-6 text-slate-500">

            Select text, or click a formula and choose “Reference in graphic.” References will appear here automatically.
          </p>
        </section>
      )}

      {error ? <p role="alert" className="rounded-xl border border-rose-200 bg-rose-50 px-3 py-2 text-[12px] leading-5 text-rose-700">{error}</p> : null}

      {scene ? (
        <section className="space-y-3 rounded-2xl border border-slate-200 bg-white p-3 shadow-sm">
          <div className="px-1">
            <div className="flex items-center justify-between gap-3">
              <h3 className="text-sm font-semibold text-slate-900">{scene.title}</h3>
              <span className="rounded-full bg-slate-100 px-2 py-1 text-[9px] font-bold uppercase tracking-wider text-slate-500">{scene.dimension}</span>
            </div>
            {scene.summary ? <p className="mt-1.5 text-[11px] leading-5 text-slate-500">{scene.summary}</p> : null}
          </div>
          <GeometrySceneViewer scene={scene} />
          {scene.steps.length ? (
            <ol className="space-y-2 px-1 pb-1">
              {scene.steps.map((step, index) => (
                <li key={`${index}-${step}`} className="flex gap-2 text-[11px] leading-5 text-slate-600">
                  <span className="mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-slate-100 text-[9px] font-bold text-slate-500">{index + 1}</span>
                  <span>{step}</span>
                </li>
              ))}
            </ol>
          ) : null}
        </section>
      ) : null}
    </div>
  );
}
