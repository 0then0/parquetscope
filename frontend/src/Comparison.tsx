import { useMemo, useState } from "react";
import { comparabilityNotes, compareComposition, readDifferences } from "./compare";
import { bytes, exact, fileTitle, isSetRun, type Run, rangeText } from "./model";
import { Pagination } from "./Pagination";

const PAGE = 20;

export function Comparison({
  a,
  b,
  runLabels = ["A", "B"],
}: {
  a: Run;
  b: Run;
  runLabels?: string[];
}) {
  const [page, setPage] = useState(0);
  const composition = useMemo(() => compareComposition(a, b), [a, b]);
  const differences = useMemo(() => readDifferences(a, b), [a, b]);
  const notes = useMemo(() => comparabilityNotes(a, b, composition), [a, b, composition]);
  const sets = isSetRun(a) || isSetRun(b);
  const labels = [
    "DuckDB engine",
    "Prefetch disabled (requested → engine)",
    "GET requests",
    "HEAD requests",
    "Confirmed ranges",
    "Unique coverage",
    "Repeated coverage",
    "Server wrote",
    "Server records (GET / HEAD)",
    "SELECT duration",
    "Files with GET",
    "Files HEAD only",
    "Files without observed requests",
  ];
  const serverGets = (r: Run) => r.server_observations.filter((s) => s.method === "GET").length;
  const values = (r: Run) => [
    `${r.duckdb_version} · httpfs ${r.engine?.httpfs_version ?? "unknown"}`,
    r.engine?.prefetch
      ? `${r.engine.prefetch.requested === null ? "default" : String(r.engine.prefetch.requested)} → ${r.engine.prefetch.applied ?? "unread"}`
      : "Not recorded",
    String(r.metrics.get_count),
    String(r.metrics.head_count),
    bytes(r.metrics.confirmed_range_bytes),
    bytes(r.metrics.unique_coverage_bytes),
    bytes(r.metrics.repeated_coverage_bytes),
    bytes(r.metrics.server_written_bytes),
    `${serverGets(r)} / ${r.server_observations.length - serverGets(r)}`,
    r.query_duration_ms === null ? "Unknown" : `${r.query_duration_ms.toFixed(1)} ms`,
    `${r.metrics.files_with_get} of ${r.files.length}`,
    String(r.metrics.files_head_only),
    String(r.metrics.files_without_observed_access),
  ];
  const av = values(a),
    bv = values(b);
  const places = (files: typeof a.files) => (files.length ? files.map(fileTitle).join(", ") : "—");
  return (
    <section className="panel compare-panel">
      <div className="panel-heading">
        <div>
          <h2>Run comparison</h2>
          <p>
            {composition.identicalContent
              ? `Identical file bytes (SHA-256)${composition.sameLocations ? " at identical locations" : " at different logical locations"}. Read options and path-derived columns can still differ.`
              : "Different file bytes. Schema similarity does not establish data equivalence."}
          </p>
        </div>
        <span className="tag">
          {runLabels[0]} ↔ {runLabels[1]}
        </span>
      </div>
      <div className="compare-grid">
        <div className="compare-labels">
          <h3>Observed metric</h3>
          {labels.map((l) => (
            <div key={l}>{l}</div>
          ))}
        </div>
        {[a, b].map((r, i) => (
          <div className="compare-values" key={r.id}>
            <h3>
              <span className="run-letter">{runLabels[i]}</span>
              {r.label}
            </h3>
            {(i ? bv : av).map((v, j) => (
              <div key={labels[j]} className={av[j] !== bv[j] ? "difference" : ""}>
                {v}
              </div>
            ))}
          </div>
        ))}
      </div>
      {sets && (
        <>
          <div className="panel-heading compare-subheading">
            <div>
              <h3>Files matched by content</h3>
              <p>
                Rows pair files with identical bytes, wherever they sit in each set. Requests are
                summed per side.
              </p>
            </div>
          </div>
          <div className="table-scroll">
            <table className="compare-files">
              <thead>
                <tr>
                  <th>Content (SHA-256)</th>
                  <th>{runLabels[0]} location</th>
                  <th>GET / HEAD</th>
                  <th>Server wrote</th>
                  <th>{runLabels[1]} location</th>
                  <th>GET / HEAD</th>
                  <th>Server wrote</th>
                </tr>
              </thead>
              <tbody>
                {composition.rows.slice(page * PAGE, (page + 1) * PAGE).map((row) => (
                  <tr key={row.sha256 + (row.a[0]?.id ?? "") + (row.b[0]?.id ?? "")}>
                    <td>
                      <code title={row.sha256}>{row.sha256.slice(0, 12)}</code>
                    </td>
                    <td>{places(row.a)}</td>
                    <td>{row.a.length ? `${row.aGet} / ${row.aHead}` : "—"}</td>
                    <td title={exact(row.aWrote.toString())}>
                      {row.a.length ? bytes(row.aWrote.toString()) : "—"}
                    </td>
                    <td>{places(row.b)}</td>
                    <td>{row.b.length ? `${row.bGet} / ${row.bHead}` : "—"}</td>
                    <td title={exact(row.bWrote.toString())}>
                      {row.b.length ? bytes(row.bWrote.toString()) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={page} total={composition.rows.length} size={PAGE} onChange={setPage} />
        </>
      )}
      <div className="panel-heading compare-subheading">
        <div>
          <h3>Read parameters and engine</h3>
        </div>
      </div>
      <dl className="difference-list">
        {differences.map((d) => (
          <div key={d.label} className={d.differs ? "difference-row differs" : "difference-row"}>
            <dt>{d.label}</dt>
            <dd>
              <span className="run-letter">{runLabels[0]}</span> {d.a}
            </dd>
            <dd>
              <span className="run-letter">{runLabels[1]}</span> {d.b}
            </dd>
          </div>
        ))}
      </dl>
      <div className="panel-heading compare-subheading">
        <div>
          <h3>Comparability limits</h3>
        </div>
      </div>
      <ul className="reading-notes">
        {notes.map((n) => (
          <li key={n}>{n}</li>
        ))}
      </ul>
      <div className="comparison-context">
        {[a, b].map((r) => (
          <article key={r.id}>
            <h3>
              {r.label}{" "}
              <span className={`status ${r.capture.complete ? "ok" : "warn"}`}>
                {r.capture.complete ? "Complete" : "Partial capture"}
              </span>
            </h3>
            <pre>{r.sql}</pre>
            <dl>
              <dt>Versions</dt>
              <dd>
                ParquetScope {r.app_version} · DuckDB {r.duckdb_version} · PyArrow{" "}
                {r.pyarrow_version}
              </dd>
              <dt>Environment</dt>
              <dd>{r.platform}</dd>
              <dt>{r.files.length === 1 ? "Input identity (SHA-256)" : "Files"}</dt>
              <dd>
                {r.files.length === 1
                  ? r.files[0].sha256
                  : `${r.files.length} files · ${exact(r.files.reduce((s, f) => s + BigInt(f.size), 0n).toString())}`}
              </dd>
              {r.files.length === 1 && (
                <>
                  <dt>File size</dt>
                  <dd>{exact(r.files[0].size)}</dd>
                </>
              )}
              <dt>Settings</dt>
              <dd>
                <code>{JSON.stringify(r.settings)}</code>
              </dd>
              {r.files.length === 1 && (
                <>
                  <dt>Coverage intervals (exact, half-open)</dt>
                  <dd>
                    <details>
                      <summary>{r.metrics.coverage?.length ?? "Unknown"} intervals</summary>
                      <pre>{r.metrics.coverage?.map(rangeText).join("\n") ?? "Unknown"}</pre>
                    </details>
                  </dd>
                </>
              )}
            </dl>
          </article>
        ))}
      </div>
      <p className="footnote">
        Server wrote and server records come from the loopback listener: they are bytes written to
        the socket and requests it saw, not bytes DuckDB received or used. Confirmed ranges come
        from the engine's own log. Byte positions belong to their own file. Different layouts are
        compared by measured totals, not by matching offsets. Overlap is not automatically wasted
        work. Metrics from partial captures describe observed subsets.
      </p>
    </section>
  );
}
