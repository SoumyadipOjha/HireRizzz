import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `npm run dev` -> http://localhost:5173 with the API proxied to a local `screening serve` (port 8765).
const backend = process.env.HIRERIZZ_DEV_API || "http://127.0.0.1:8765";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": backend,
      "/interview": backend,
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
