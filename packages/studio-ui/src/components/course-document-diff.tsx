"use client";

function documentText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && "content_text" in value && typeof value.content_text === "string") return value.content_text;
  return value == null ? "" : JSON.stringify(value, null, 2);
}

export function CourseDocumentDiff({ before, after, beforeLabel = "Base document", afterLabel = "Result document" }: { before: unknown; after: unknown; beforeLabel?: string; afterLabel?: string }) {
  const oldLines = documentText(before).split("\n");
  const newLines = documentText(after).split("\n");
  return <section aria-label="Document changes" className="mt-4">
    <p className="mb-2 text-xs text-stone-500">Document changes are compared separately. Removing text does not delete history nodes.</p>
    <div className="grid gap-3 md:grid-cols-2">{[{ label: beforeLabel, lines: oldLines, other: newLines, tone: "bg-rose-50" }, { label: afterLabel, lines: newLines, other: oldLines, tone: "bg-emerald-50" }].map(({ label, lines, other, tone }) => <div key={label} className="min-w-0 overflow-hidden rounded-lg border border-stone-200"><h4 className="border-b bg-stone-50 px-3 py-2 text-xs font-semibold">{label}</h4><pre className="max-h-80 overflow-auto p-3 text-xs leading-6">{lines.map((line, index) => <span key={index} className={`block whitespace-pre-wrap break-words ${line !== other[index] ? tone : ""}`}><span className="mr-3 select-none text-stone-400">{index + 1}</span>{line || " "}</span>)}</pre></div>)}</div>
  </section>;
}
