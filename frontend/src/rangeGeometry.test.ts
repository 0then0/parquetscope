import { expect, test } from "vitest";
import { position, tick } from "./rangeGeometry";

test("zoom keeps adjacent bytes distinct beyond Number.MAX_SAFE_INTEGER", () => {
  const domain = { start: "9007199254740993000", end: "9007199254740993010" };
  expect(position("9007199254740993001", domain)).toBe(0.1);
  expect(position("9007199254740993002", domain)).toBe(0.2);
  expect(tick(domain, 1, 4)).toBe("9007199254740993002");
  expect(tick(domain, 4, 4)).toBe(domain.end);
});
test("positions outside the viewport remain outside for clipping", () => {
  const domain = { start: "100", end: "200" };
  expect(position("50", domain)).toBe(-0.5);
  expect(position("250", domain)).toBe(1.5);
  expect(position("100", { start: "100", end: "100" })).toBe(0);
});
