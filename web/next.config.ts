import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // Enable React Compiler for automatic memoization
  reactCompiler: true,

  // Standalone output for Docker deployment
  output: 'standalone',

  // Proxy API requests to the Sibyl backend during development
  async rewrites() {
    return [
      {
        source: '/api/:path*',
        destination: 'http://localhost:3334/api/:path*',
      },
      {
        source: '/ws',
        destination: 'http://localhost:3334/api/ws',
      },
    ];
  },
};

export default nextConfig;
