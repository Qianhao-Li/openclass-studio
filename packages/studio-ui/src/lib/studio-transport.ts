let localOrigin: string | null = null;

export function configureLocalStudioTransport(origin: string) {
  const value = new URL(origin);
  if (value.protocol !== "http:" || !["localhost", "127.0.0.1", "[::1]"].includes(value.hostname)
      || value.username || value.password || value.pathname !== "/" || value.search || value.hash) {
    throw new Error("Local Studio requires a loopback API origin.");
  }
  localOrigin = value.origin;
}

export function localStudioApiOrigin() { return localOrigin; }
