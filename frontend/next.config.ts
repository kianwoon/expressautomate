import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Static export: the landing page needs no server, so it ships as files
  // behind nginx on the cheapest Koyeb instance. Revisit when the app UI
  // needs SSR or route handlers.
  output: "export",
  images: { unoptimized: true },
  trailingSlash: true,
};

export default nextConfig;
