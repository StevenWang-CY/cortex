// Ambient type shims for the extension's TypeScript build.
//
// `process` is the Node.js global in vitest (test runtime) and is
// polyfilled by Plasmo's bundler in the extension build. We declare
// just enough of it to satisfy the typecheck without pulling in the
// full `@types/node` package — runtime guards
// (`typeof process !== "undefined"`) handle the rare cases where
// neither layer provides it.

declare const process: {
    env: Record<string, string | undefined>;
};
