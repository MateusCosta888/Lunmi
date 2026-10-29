import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true
  },
  test: {
    environment: "jsdom",
    // React 19 only ships `act` in its development build; a machine with NODE_ENV=production broke every render test.
    env: { NODE_ENV: "test" },
    setupFiles: ["tests/setup.ts"],
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"]
  }
});
