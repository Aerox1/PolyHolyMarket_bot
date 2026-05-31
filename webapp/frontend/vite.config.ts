import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// SPA is served from the FastAPI app at the same origin under "/".
// API calls use relative URLs (/api/...), so base must be "/".
export default defineConfig({
  base: "/",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
