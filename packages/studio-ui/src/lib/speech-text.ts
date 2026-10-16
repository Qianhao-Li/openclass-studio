const SPEECH_INPUT_MAX_CHARACTERS = 4000;

export function prepareSpeechText(content: string): string {
  const normalized = content
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/^\s{0,3}#{1,6}\s+/gm, "")
    .replace(/^\s*>\s?/gm, "")
    .replace(/^\s*[-*+]\s+/gm, "")
    .replace(/^\s*\d+[.)]\s+/gm, "")
    .replace(/```[^\n]*\n?/g, "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/__([^_]+)__/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    .replace(/<[^>]+>/g, " ")
    .replace(/\$\$?|\\\(|\\\)|\\\[|\\\]/g, "")
    .replace(/\|/g, "，")
    .replace(/[ \t]+/g, " ")
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  if (normalized.length <= SPEECH_INPUT_MAX_CHARACTERS) {
    return normalized;
  }

  const shortened = normalized.slice(0, SPEECH_INPUT_MAX_CHARACTERS);
  const sentenceBoundary = Math.max(
    shortened.lastIndexOf("。"),
    shortened.lastIndexOf("！"),
    shortened.lastIndexOf("？"),
    shortened.lastIndexOf("\n")
  );
  return (sentenceBoundary >= 1000 ? shortened.slice(0, sentenceBoundary + 1) : shortened).trim();
}
