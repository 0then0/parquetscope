import type { Settings } from "./model";

export type PrefetchChoice = "default" | "on" | "off";
const PREFETCH: Record<PrefetchChoice, boolean | null> = { default: null, on: false, off: true };

/**
 * Body of POST /api/runs. Engine and prefetch apply to file sets only: for any other input the
 * server rejects them, so a leftover selection must not travel with a single-file run.
 */
export function runRequest(
  inputId: string,
  kind: "file" | "set" | undefined,
  sql: string,
  settings: Settings,
  engine: string,
  prefetch: PrefetchChoice,
) {
  if (kind !== "set") return { input_id: inputId, sql, settings };
  return {
    input_id: inputId,
    sql,
    engine: engine || null,
    settings: { ...settings, disable_parquet_prefetching: PREFETCH[prefetch] },
  };
}
