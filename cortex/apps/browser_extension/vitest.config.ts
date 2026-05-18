import { defineConfig } from "vitest/config";

export default defineConfig({
    test: {
        environment: "jsdom",
        globals: true,
        setupFiles: ["./test/setup.ts"],
        include: ["__tests__/**/*.spec.{ts,tsx}"],
        // Exclude the live extension source from being treated as tests; it
        // is imported explicitly by individual specs.
        exclude: ["node_modules/**", ".plasmo/**", "build/**"],
    },
});
