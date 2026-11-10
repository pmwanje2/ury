import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { parse } from "@vue/compiler-sfc";

// The kitchen header logo must match the POS header: a fixed height with an
// automatic width. A tenant logo (Website Settings) is often square, and the
// old fixed width w-40 with the non-existent h-15 class let it render at its
// full 160 px width and natural height, pushing the board down.
const source = await readFile(new URL("./Header.vue", import.meta.url), "utf8");
const template = parse(source).descriptor.template.content;
const img = template.match(/<img\b[^>]*:src="brand\.logo"[^>]*>/);

test("kitchen header logo is height-bounded like the POS header", () => {
  assert.ok(img, "header renders the brand logo");
  const classes = (img[0].match(/class="([^"]*)"/) || [])[1]?.split(/\s+/) ?? [];
  assert.ok(classes.includes("h-10"), `logo has a fixed height (got: ${classes.join(" ")})`);
  assert.ok(classes.includes("w-auto"), `logo width follows its aspect ratio (got: ${classes.join(" ")})`);
  for (const unbounded of ["w-40", "h-15", "ml-20"]) {
    assert.ok(!classes.includes(unbounded), `logo does not use ${unbounded}`);
  }
});
