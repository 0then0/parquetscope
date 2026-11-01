import { useMemo, useState } from "react";
import {
  type Access,
  bytes,
  exact,
  type FileEntry,
  type FileMetrics,
  fileTitle,
  type Run,
} from "./model";
import { Pagination } from "./Pagination";

type Filter = "all" | "observed" | "none" | "head_only" | "get";
type SortKey = "path" | "size" | "requests" | "bytes";
type Row = { file: FileEntry; metrics: FileMetrics; binding: number | null };
const PAGE = 25;
const FILTERS: Record<Filter, string> = {
  all: "All files",
  observed: "With observed requests",
  none: "No observed requests",
  head_only: "HEAD only",
  get: "With GET",
};

export function accessLabel(access: Access, complete: boolean): string {
  if (access === "get") return "GET observed";
  if (access === "head_only") return "HEAD only";
  return complete ? "No observed requests" : "No observed requests (partial trace)";
}

export function FileOverview({
  run,
  selectedId,
  onInspect,
}: {
  run: Run;
  selectedId: string;
  onInspect: (fileId: string) => void;
}) {
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortKey>("path");
  const [descending, setDescending] = useState(false);
  const [page, setPage] = useState(0);
  const phased = run.schema_version === "2";
  const rows = useMemo<Row[]>(() => {
    const metrics = new Map(run.file_metrics.map((m) => [m.file_id, m]));
    return run.files.flatMap((file) => {
      const m = metrics.get(file.id);
      if (!m) return [];
      const observations = run.byFile.get(file.id)?.observations ?? [];
      return [
        {
          file,
          metrics: m,
          binding: phased ? observations.filter((o) => o.phase === "view_creation").length : null,
        },
      ];
    });
  }, [run, phased]);
  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    const wanted = rows.filter(({ file, metrics }) => {
      if (needle && !fileTitle(file).toLowerCase().includes(needle)) return false;
      if (filter === "observed") return metrics.observed_access !== "none";
      return filter === "all" || metrics.observed_access === filter;
    });
    const key = (r: Row): bigint | string => {
      if (sort === "size") return BigInt(r.file.size);
      if (sort === "requests") return BigInt(r.metrics.get_count + r.metrics.head_count);
      if (sort === "bytes") return BigInt(r.metrics.server_written_bytes);
      return fileTitle(r.file);
    };
    const sign = descending ? -1 : 1;
    return [...wanted].sort((a, b) => {
      const x = key(a);
      const y = key(b);
      return x < y ? -sign : x > y ? sign : fileTitle(a.file).localeCompare(fileTitle(b.file));
    });
  }, [rows, filter, search, sort, descending]);
  const m = run.metrics;
  const header = (label: string, id: SortKey) => (
    <th aria-sort={sort === id ? (descending ? "descending" : "ascending") : "none"}>
      <button
        type="button"
        className="text-button sort-button"
        onClick={() => {
          setDescending(sort === id ? !descending : false);
          setSort(id);
          setPage(0);
        }}
      >
        {label}
        {sort === id ? (descending ? " ↓" : " ↑") : ""}
      </button>
    </th>
  );
  return (
    <section className="panel file-overview">
      <div className="panel-heading">
        <div>
          <h2>File set overview</h2>
          <p>
            {run.files.length} files · <b>{m.files_with_get}</b> with GET ·{" "}
            <b>{m.files_head_only}</b> HEAD only · <b>{m.files_without_observed_access}</b> without
            observed requests
            {run.read && <> · hive_partitioning {run.read.hive_partitioning ? "on" : "off"}</>}
          </p>
        </div>
        <span className={`status ${run.capture.complete ? "ok" : "warn"}`}>
          {run.capture.complete ? "● Complete capture" : "◐ Partial capture"}
        </span>
      </div>
      <div className="file-toolbar">
        <label>
          Show
          <select
            aria-label="Filter files by observed access"
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value as Filter);
              setPage(0);
            }}
          >
            {(Object.keys(FILTERS) as Filter[]).map((id) => (
              <option key={id} value={id}>
                {FILTERS[id]}
              </option>
            ))}
          </select>
        </label>
        <input
          aria-label="Filter files by path"
          placeholder="Filter by path or partition…"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(0);
          }}
        />
        <span className="micro">
          {visible.length} of {rows.length} files
        </span>
      </div>
      <div className="table-scroll">
        <table className="file-table">
          <thead>
            <tr>
              {header("File", "path")}
              <th>Partition segments</th>
              {header("Size", "size")}
              {header("Requests", "requests")}
              {phased && <th title="Requests during view creation (input binding)">In binding</th>}
              {header("Server wrote", "bytes")}
              <th>Unique coverage</th>
              <th>Observed access</th>
              <th>Inspect</th>
            </tr>
          </thead>
          <tbody>
            {visible.slice(page * PAGE, (page + 1) * PAGE).map(({ file, metrics, binding }) => {
              const slash = fileTitle(file).lastIndexOf("/");
              const directory = slash >= 0 ? fileTitle(file).slice(0, slash + 1) : "";
              return (
                <tr key={file.id} className={file.id === selectedId ? "selected" : ""}>
                  <td className="file-name">
                    <span className="file-dir">{directory}</span>
                    <b>{file.name}</b>
                  </td>
                  <td>
                    {file.partition.length ? (
                      file.partition.map((p) => (
                        <span className="chip" key={`${p.key}=${p.value}`}>
                          {p.key}={p.value}
                        </span>
                      ))
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td title={exact(file.size)}>{bytes(file.size)}</td>
                  <td>
                    {metrics.get_count} GET / {metrics.head_count} HEAD
                  </td>
                  {phased && <td>{binding}</td>}
                  <td title={exact(metrics.server_written_bytes)}>
                    {bytes(metrics.server_written_bytes)}
                  </td>
                  <td title={exact(metrics.unique_coverage_bytes)}>
                    {bytes(metrics.unique_coverage_bytes)}
                  </td>
                  <td>
                    <span className={`access access-${metrics.observed_access}`}>
                      {accessLabel(metrics.observed_access, run.capture.complete)}
                    </span>
                  </td>
                  <td>
                    <button
                      type="button"
                      aria-label={`Inspect ${fileTitle(file)}`}
                      onClick={() => onInspect(file.id)}
                    >
                      Inspect
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Pagination page={page} total={visible.length} size={PAGE} onChange={setPage} />
      <ul className="reading-notes">
        <li>
          “No observed requests” means this capture recorded none for the file.
          {!run.capture.complete &&
            " The trace is partial, so it does not prove the file was never accessed."}
        </li>
        <li>
          A range that intersects a column chunk or the footer does not show that the column was
          decoded or that the query read only metadata.
        </li>
        <li>
          Server wrote counts bytes streamed by the loopback server, not bytes DuckDB confirmed
          using. A small result does not imply small reads.
        </li>
        <li>Partition segments are key=value directory names in the path, shown as written.</li>
      </ul>
    </section>
  );
}
