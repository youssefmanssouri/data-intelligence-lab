import { SECURITY_HEADERS } from "./config/proxy.mjs";
/** @type {import('next').NextConfig} */
const config = {
  poweredByHeader: false,
  logging: { incomingRequests: false },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: Object.entries(SECURITY_HEADERS).map(([key, value]) => ({
          key,
          value,
        })),
      },
      ...["/api/:path*", "/health", "/ready", "/datasets/:path*"].map(
        (source) => ({
          source,
          headers: [{ key: "Cache-Control", value: "no-store, max-age=0" }],
        }),
      ),
    ];
  },
};
export default config;
