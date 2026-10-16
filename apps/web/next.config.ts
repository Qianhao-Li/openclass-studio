import path from "node:path";
import type { NextConfig } from "next";
const config: NextConfig = {
 transpilePackages: ["@openclass/studio"],
 distDir: process.env.OPENCLASS_NEXT_DIST_DIR || ".next-studio",
 outputFileTracingRoot: path.resolve(__dirname, "../.."),
 allowedDevOrigins: ["localhost", "127.0.0.1"],
 async headers() {
  const api = process.env.OPENCLASS_STUDIO_API_ORIGIN || "http://127.0.0.1:8510";
  const development = process.env.NODE_ENV === "development";
  return [{ source: "/:path*", headers: [
   { key: "X-Content-Type-Options", value: "nosniff" },
   { key: "Referrer-Policy", value: "no-referrer" },
   { key: "X-Frame-Options", value: "DENY" },
   { key: "Content-Security-Policy", value: `default-src 'self'; script-src 'self' 'unsafe-inline' ${development ? "'unsafe-eval'" : ""}; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob: https: ${api}; font-src 'self' data:; connect-src 'self' ${api} ${api.replace("http:", "ws:")}; media-src 'self' data: blob: ${api}; frame-src 'self' https:; worker-src 'self' blob:; object-src 'none'; base-uri 'self'; form-action 'self'` },
  ] }];
 }
};
export default config;
