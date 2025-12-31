import type { MetadataRoute } from "next";

export default function manifest(): MetadataRoute.Manifest {
  return {
    name: "Sibyl",
    short_name: "Sibyl",
    description: "Collective Intelligence Runtime - shared memory for AI agents",
    start_url: "/",
    display: "standalone",
    background_color: "#0a0a0f",
    theme_color: "#e135ff",
    icons: [
      {
        src: "/sibyl-icon-192.png",
        sizes: "192x192",
        type: "image/png",
      },
      {
        src: "/sibyl-icon-512.png",
        sizes: "512x512",
        type: "image/png",
      },
    ],
  };
}
