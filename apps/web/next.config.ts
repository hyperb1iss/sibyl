import type { NextConfig } from 'next';

// Backend URL for development rewrites (when not using Kong/ingress)
// In production with Kong, these rewrites are bypassed - Kong routes /api/* to backend
const BACKEND_URL = process.env.SIBYL_BACKEND_URL || 'http://localhost:3334';

const nextConfig: NextConfig = {
  // Enable React Compiler for automatic memoization
  reactCompiler: true,

  // Standalone output for Docker deployment
  output: 'standalone',

  // Proxy API requests to the Sibyl backend during local development
  // Note: When deployed behind Kong/ingress, routing is handled externally
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: `${BACKEND_URL}/api/:path*`,
      },
      {
        source: '/ws',
        destination: `${BACKEND_URL}/api/ws`,
      },
    ];
  },
};

export default nextConfig;
