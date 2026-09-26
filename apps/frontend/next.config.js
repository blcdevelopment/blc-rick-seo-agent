/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Emit a self-contained server bundle (.next/standalone/server.js) for a small
  // production Docker image — see apps/frontend/Dockerfile.
  output: "standalone"
};

module.exports = nextConfig;
