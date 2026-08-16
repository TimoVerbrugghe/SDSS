import deckyPlugin from "@decky/rollup";

const config = deckyPlugin({});

// Only dist/index.js is committed and installed (SteamOS has no node, so the bundle ships
// prebuilt). The Decky preset enables sourcemaps unconditionally and its options are
// merged so they win over anything passed in, hence this override after the fact --
// otherwise the bundle carries a `//# sourceMappingURL=index.js.map` comment pointing at
// a file that is never deployed, producing a 404 on every panel open.
config.output.sourcemap = false;
delete config.output.sourcemapPathTransform;

export default config;
