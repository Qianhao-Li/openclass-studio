export function decodeBase64Bytes(base64: string): Uint8Array {
  const binary = window.atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

function encodeBase64Bytes(bytes: Uint8Array): string {
  let binary = "";
  const chunkSize = 0x8000;
  for (let index = 0; index < bytes.length; index += chunkSize) {
    const chunk = bytes.subarray(index, index + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return window.btoa(binary);
}

function parsePcmSampleRate(mimeType?: string): number {
  const match = mimeType?.match(/rate=(\d+)/i);
  return match ? Number(match[1]) : 24000;
}

export function resampleLinear(input: Float32Array, sourceRate: number, targetRate: number): Float32Array {
  if (sourceRate === targetRate) {
    return input;
  }
  const outputLength = Math.max(1, Math.round((input.length * targetRate) / sourceRate));
  const output = new Float32Array(outputLength);
  const ratio = (input.length - 1) / Math.max(1, outputLength - 1);
  for (let index = 0; index < outputLength; index += 1) {
    const sourceIndex = index * ratio;
    const left = Math.floor(sourceIndex);
    const right = Math.min(input.length - 1, left + 1);
    const weight = sourceIndex - left;
    output[index] = input[left] * (1 - weight) + input[right] * weight;
  }
  return output;
}

export function pcmFloatToBase64(input: Float32Array): string {
  const bytes = new Uint8Array(input.length * 2);
  const view = new DataView(bytes.buffer);
  input.forEach((sample, index) => {
    const clamped = Math.max(-1, Math.min(1, sample));
    view.setInt16(index * 2, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
  });
  return encodeBase64Bytes(bytes);
}

export function playPcmBase64(
  base64: string,
  mimeType: string | undefined,
  audioContext: AudioContext,
  playbackTimeRef: { current: number }
): AudioBufferSourceNode {
  const bytes = decodeBase64Bytes(base64);
  const view = new DataView(bytes.buffer);
  const sampleCount = Math.floor(bytes.byteLength / 2);
  const sampleRate = parsePcmSampleRate(mimeType);
  const buffer = audioContext.createBuffer(1, sampleCount, sampleRate);
  const output = buffer.getChannelData(0);
  for (let index = 0; index < sampleCount; index += 1) {
    output[index] = view.getInt16(index * 2, true) / 0x8000;
  }
  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(audioContext.destination);
  const startAt = Math.max(audioContext.currentTime, playbackTimeRef.current);
  source.start(startAt);
  playbackTimeRef.current = startAt + buffer.duration;
  return source;
}
