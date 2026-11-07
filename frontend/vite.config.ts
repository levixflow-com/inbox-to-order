import { resolve } from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The build is served by the Python process (inbox-to-order web): no Node needed to run the app.
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: resolve(__dirname, "../src/inbox_to_order/web/dist"),
    emptyOutDir: true,
    rollupOptions: {
      input: { index: resolve(__dirname, "index.html"), erp: resolve(__dirname, "erp.html") },
    },
  },
});
