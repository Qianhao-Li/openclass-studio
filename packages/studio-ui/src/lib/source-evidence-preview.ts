type EvidenceView = { source_title: string; text: string; pages: { page: number | null; data_url: string }[] };
type ScopedClient = { request: <T>(path: string, init?: RequestInit) => Promise<T> };

function isRasterDataUrl(value: string): boolean {
  const header = /^data:image\/(?:png|jpeg|webp|gif);base64,/.exec(value);
  if (!header) return false;
  const encoded = value.slice(header[0].length);
  return encoded.length > 0 && encoded.length % 4 === 0 && /^[A-Za-z0-9+/]*={0,2}$/.test(encoded);
}

export async function openSourceEvidencePreview(api: ScopedClient, packageId: string, lessonId: string, evidenceRef: string) {
  if (!/^[a-f0-9]{64}$/.test(evidenceRef)) throw new Error("The saved citation identifier is invalid.");
  const preview = window.open("", "_blank");
  if (!preview) throw new Error("Allow pop-up windows to view this course citation.");
  preview.opener = null;
  preview.document.documentElement.lang = "en";
  preview.document.documentElement.translate = false;
  preview.document.title = "Course citation";
  preview.document.body.textContent = "Loading the saved course citation…";
  try {
    const result = await api.request<EvidenceView>(`/api/packages/${encodeURIComponent(packageId)}/lessons/${encodeURIComponent(lessonId)}/source-evidence/${evidenceRef}?format=json`);
    if (preview.closed) return;
    const doc = preview.document;
    doc.title = result.source_title;
    doc.body.replaceChildren();
    doc.body.style.cssText = "max-width:960px;margin:32px auto;padding:0 20px;font:16px/1.7 system-ui";
    const title = doc.createElement("h1"); title.textContent = result.source_title;
    const note = doc.createElement("p"); note.textContent = "Source citation saved with this course";
    const text = doc.createElement("pre"); text.style.whiteSpace = "pre-wrap"; text.textContent = result.text;
    doc.body.append(title, note, text);
    for (const page of result.pages) {
      if (!isRasterDataUrl(page.data_url)) throw new Error("The citation image format is invalid.");
      const figure = doc.createElement("figure");
      const image = doc.createElement("img"); image.src = page.data_url; image.style.maxWidth = "100%"; image.alt = `Page ${page.page ?? ""}`;
      const caption = doc.createElement("figcaption"); caption.textContent = `Page ${page.page ?? ""}`;
      figure.append(image, caption); doc.body.append(figure);
    }
  } catch (error) { preview.close(); throw error; }
}
