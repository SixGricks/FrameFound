// Lets a test load a source file that imports another source file.
//
// Node runs the app's TypeScript as it is (types stripped) but, unlike the
// bundler, will not guess a file extension: `import … from "./chatReducer"`
// inside src/lib/chatSession.ts does not resolve. Importing this file first
// teaches it the one guess that is needed — a relative import made from a
// .ts file that was not found is tried again with ".ts" on the end.
//
// It takes effect for imports made after it has run, so a test that needs
// it loads its subject with `await import(…)` below its own imports, not
// with an `import` statement (those are all resolved before any code runs).
//
// Not a test itself: `npm test` only runs tests/*.test.mjs.

import module from "node:module";

if (typeof module.registerHooks !== "function") {
  throw new Error("These tests need Node 22.15 or newer (module.registerHooks).");
}

module.registerHooks({
  resolve(specifier, context, nextResolve) {
    try {
      return nextResolve(specifier, context);
    } catch (err) {
      const relative = specifier.startsWith("./") || specifier.startsWith("../");
      if (err?.code === "ERR_MODULE_NOT_FOUND" && relative && context.parentURL?.endsWith(".ts")) {
        return nextResolve(`${specifier}.ts`, context);
      }
      throw err;
    }
  },
});
