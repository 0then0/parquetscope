import type { Span } from "./model";

// Subtract exact byte offsets before converting a bounded ratio to pixels.
export function position(offset: string, domain: Span): number {
  const start = BigInt(domain.start);
  const width = BigInt(domain.end) - start;
  if (width <= 0n) return 0;
  return Number(((BigInt(offset) - start) * 1000000000n) / width) / 1000000000;
}
export function tick(domain: Span, index: number, count: number): string {
  return (
    BigInt(domain.start) +
    ((BigInt(domain.end) - BigInt(domain.start)) * BigInt(index)) / BigInt(count)
  ).toString();
}
