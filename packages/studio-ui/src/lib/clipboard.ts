function copyTextWithSelection(text: string): boolean {
  const activeElement = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.readOnly = true;
  textarea.setAttribute("aria-hidden", "true");
  textarea.style.position = "fixed";
  textarea.style.inset = "0 auto auto -9999px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  textarea.setSelectionRange(0, text.length);

  try {
    return document.execCommand("copy");
  } finally {
    textarea.remove();
    activeElement?.focus({ preventScroll: true });
  }
}

export async function writeTextToClipboard(text: string): Promise<boolean> {
  if (!text) {
    return false;
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // A browser may expose the Clipboard API but reject it because of page or
    // permission state. Preserve the click flow for the selection fallback.
  }
  try {
    return copyTextWithSelection(text);
  } catch {
    return false;
  }
}
