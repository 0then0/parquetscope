import { readFile, writeFile } from "node:fs/promises";

const root = new URL("../../src/parquetscope/static/", import.meta.url);
let html = await readFile(new URL("index.html", root), "utf8");
const js = /<script type="module"[^>]*src="([^"]+)"[^>]*><\/script>/g;
for (const match of [...html.matchAll(js)]) {
  const source = await readFile(new URL(match[1].slice(1), root), "utf8");
  html = html.replace(
    match[0],
    () => `<script type="module">${source.replaceAll("</script", "<\\/script")}</script>`,
  );
}
const css = /<link[^>]*href="([^"]+\.css)"[^>]*>/g;
for (const match of [...html.matchAll(css)]) {
  const source = await readFile(new URL(match[1].slice(1), root), "utf8");
  html = html.replace(match[0], () => `<style>${source}</style>`);
}
await writeFile(new URL("offline.html", root), html);
console.log("Self-contained offline.html built");
