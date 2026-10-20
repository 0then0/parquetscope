import { useCallback, useEffect, useRef, useState } from "react";
import projectIconSvg from "../../docs/icon.svg?raw";
import { type LabData, LabDetails, LabIntro } from "./Lab";
import {
  bytes,
  exact,
  intersects,
  MAX_BYTES,
  parseArtifact,
  type Run,
  rangeText,
  type Settings,
  type Structure,
} from "./model";
import { RangeLab } from "./RangeLab";

type InputFile = { id: string; name: string };
type Preview = { columns: string[]; rows: (string | boolean | null)[][] };
type Session = { inputs: InputFile[]; runs: Run[]; demo_available: boolean };
type AnalysisView = "trace" | "structure" | "compare" | "preview";
const DEFAULT_SQL = "SELECT *\nFROM input\nWHERE true";
const DEFAULT_SETTINGS: Settings = {
  threads: 4,
  memory_limit_mb: 512,
  deadline_seconds: 30,
  preview_rows: 30,
  preview_bytes: 65536,
  external_file_cache: false,
  cold_client: true,
};
async function api<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(
    url,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const value = await response.json();
  if (!response.ok)
    throw new Error(typeof value.detail === "string" ? value.detail : "Request failed");
  return value;
}
function download(text: string, name: string, mime = "application/json") {
  const url = URL.createObjectURL(new Blob([text], { type: mime }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
const projectIcon = `data:image/svg+xml,${encodeURIComponent(projectIconSvg)}`;

function Mark() {
  return (
    <img className="project-mark" src={projectIcon} width="30" height="30" alt="ParquetScope" />
  );
}
function Pagination({
  page,
  total,
  size,
  onChange,
}: {
  page: number;
  total: number;
  size: number;
  onChange: (n: number) => void;
}) {
  const pages = Math.max(1, Math.ceil(total / size));
  return (
    <div className="pagination">
      <span>
        {total ? page * size + 1 : 0}–{Math.min((page + 1) * size, total)} of {total}
      </span>
      <button
        type="button"
        disabled={page === 0}
        onClick={() => onChange(page - 1)}
        aria-label="Previous page"
      >
        ←
      </button>
      <button
        type="button"
        disabled={page + 1 >= pages}
        onClick={() => onChange(page + 1)}
        aria-label="Next page"
      >
        →
      </button>
    </div>
  );
}
function Structures({
  run,
  selected,
  onSelect,
}: {
  run: Run;
  selected: Structure | null;
  onSelect: (s: Structure) => void;
}) {
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const list = run.input.structures.filter((s) =>
    `${s.label} ${s.kind}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Physical structure</h2>
          <p>
            {run.input.row_groups} row groups · {run.input.schema_fields.length} columns ·
            dictionary pages included
          </p>
        </div>
        <input
          aria-label="Filter structures"
          placeholder="Filter column or row group…"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setPage(0);
          }}
        />
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>Structure</th>
              <th>Row group</th>
              <th>Start (inclusive)</th>
              <th>End (exclusive)</th>
              <th>Compressed size</th>
              <th>Related HTTP</th>
            </tr>
          </thead>
          <tbody>
            {list.slice(page * 50, (page + 1) * 50).map((s) => (
              <tr key={s.id} className={s.id === selected?.id ? "selected" : ""}>
                <td>
                  <button type="button" className="text-button" onClick={() => onSelect(s)}>
                    {s.label}
                  </button>
                </td>
                <td>{s.row_group ?? "—"}</td>
                <td>
                  <code>{s.span.start}</code>
                </td>
                <td>
                  <code>{s.span.end}</code>
                </td>
                <td title={exact((BigInt(s.span.end) - BigInt(s.span.start)).toString())}>
                  {bytes((BigInt(s.span.end) - BigInt(s.span.start)).toString())}
                </td>
                <td>
                  {run.observations.filter((o) => intersects(o.response_span, s.span)).length}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination page={page} total={list.length} size={50} onChange={setPage} />
    </section>
  );
}
function Comparison({ a, b, runLabels = ["A", "B"] }: { a: Run; b: Run; runLabels?: string[] }) {
  const identical = a.input.sha256 === b.input.sha256;
  const labels = [
    "GET requests",
    "HEAD requests",
    "Confirmed ranges",
    "Unique coverage",
    "Repeated coverage",
    "Server wrote",
    "SELECT duration",
  ];
  const values = (r: Run) => [
    String(r.metrics.get_count),
    String(r.metrics.head_count),
    bytes(r.metrics.confirmed_range_bytes),
    bytes(r.metrics.unique_coverage_bytes),
    bytes(r.metrics.repeated_coverage_bytes),
    bytes(r.metrics.server_written_bytes),
    r.query_duration_ms === null ? "Unknown" : `${r.query_duration_ms.toFixed(1)} ms`,
  ];
  const av = values(a),
    bv = values(b);
  return (
    <section className="panel compare-panel">
      <div className="panel-heading">
        <div>
          <h2>Run comparison</h2>
          <p>
            {identical
              ? "Identical input bytes (SHA-256)"
              : "Different input bytes. Schema similarity does not establish data equivalence."}
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
              {r.input.name}
            </h3>
            {(i ? bv : av).map((v, j) => (
              <div key={labels[j]} className={av[j] !== bv[j] ? "difference" : ""}>
                {v}
              </div>
            ))}
          </div>
        ))}
      </div>
      <div className="comparison-context">
        {[a, b].map((r) => (
          <article key={r.id}>
            <h3>
              {r.input.name}{" "}
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
              <dt>Input identity (SHA-256)</dt>
              <dd>{r.input.sha256}</dd>
              <dt>File size</dt>
              <dd>{exact(r.input.size)}</dd>
              <dt>Settings</dt>
              <dd>
                <code>{JSON.stringify(r.settings)}</code>
              </dd>
              <dt>Coverage intervals (exact, half-open)</dt>
              <dd>
                <details>
                  <summary>{r.metrics.coverage?.length ?? "Unknown"} intervals</summary>
                  <pre>{r.metrics.coverage?.map(rangeText).join("\n") ?? "Unknown"}</pre>
                </details>
              </dd>
            </dl>
          </article>
        ))}
      </div>
      <p className="footnote">
        Byte positions belong to their own file. Different layouts are compared by measured totals,
        not by matching offsets. Overlap is not automatically wasted work. Metrics from partial
        captures describe observed subsets.
      </p>
    </section>
  );
}
export default function App({ lab }: { lab?: LabData }) {
  const [initial] = useState(() => {
    try {
      const text = document.getElementById("parquetscope-artifact")?.textContent;
      return { run: text && text !== "null" ? parseArtifact(text) : null, error: "" };
    } catch (error) {
      return { run: null, error: `Cannot open embedded artifact: ${String(error)}` };
    }
  });
  const embedded = initial.run;
  const [runs, setRuns] = useState<Run[]>(lab?.runs ?? (embedded ? [embedded] : []));
  const [active, setActive] = useState(lab?.runs[0].id ?? embedded?.id ?? "");
  const [inputs, setInputs] = useState<InputFile[]>([]);
  const [inputId, setInputId] = useState("");
  const [sql, setSql] = useState(embedded?.sql ?? DEFAULT_SQL);
  const [settings, setSettings] = useState(DEFAULT_SETTINGS);
  const [view, setView] = useState<AnalysisView>("trace");
  const [requestId, setRequestId] = useState<string | null>(null);
  const [chunk, setChunk] = useState<Structure | null>(null);
  const [compareId, setCompareId] = useState("");
  const [busy, setBusy] = useState(false);
  const [jobId, setJobId] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState(initial.error);
  const [connected, setConnected] = useState(false);
  const [demoAvailable, setDemoAvailable] = useState(false);
  const [preview, setPreview] = useState<Record<string, Preview>>({});
  const [theme, setTheme] = useState(() => {
    try {
      return localStorage.getItem("parquetscope-theme") ?? "system";
    } catch {
      return "system";
    }
  });
  const template = useRef("");
  const stopped = useRef(false);
  const mounted = useRef(true);
  const fileRef = useRef<HTMLInputElement>(null);
  const run = runs.find((r) => r.id === active);
  const other =
    runs.find((r) => r.id === compareId && r.id !== active) ?? runs.find((r) => r.id !== active);
  const navigate = useCallback(
    (next: Run, nextView: AnalysisView = "trace", comparison?: Run, updateHistory = true) => {
      setActive(next.id);
      setSql(next.sql);
      setView(nextView);
      if (comparison) setCompareId(comparison.id);
      if (!lab || !updateHistory) return;
      const index = lab.runs.findIndex((r) => r.id === next.id);
      if (index < 0) return;
      const key = lab.report.layouts[index].id;
      const otherIndex = lab.runs.findIndex((r) => r.id === comparison?.id);
      const hash =
        nextView === "compare" && otherIndex >= 0
          ? `#compare-${key}-${lab.report.layouts[otherIndex].id}`
          : `#${key}${nextView === "trace" ? "" : `-${nextView}`}`;
      // Apply state even for the current hash; pushState avoids a second hashchange transition.
      if (location.hash !== hash) history.pushState(null, "", hash);
    },
    [lab],
  );
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("parquetscope-theme", theme);
    } catch {}
  }, [theme]);
  useEffect(() => {
    mounted.current = true;
    if (lab) {
      fetch("./offline.html")
        .then((r) => (r.ok ? r.text() : ""))
        .then((t) => {
          template.current = t;
        })
        .catch(() => {});
    }
    if (!lab && !embedded && location.protocol !== "file:") {
      api<Session>("/api/session")
        .then((s) => {
          if (!mounted.current) return;
          setConnected(true);
          setInputs(s.inputs);
          setInputId(s.inputs[0]?.id ?? "");
          setRuns(s.runs);
          setActive(s.runs[0]?.id ?? "");
          setSql(s.runs[0]?.sql ?? DEFAULT_SQL);
          setDemoAvailable(s.demo_available);
        })
        .catch(() => {});
      fetch("/offline.html")
        .then((r) => (r.ok ? r.text() : ""))
        .then((t) => {
          template.current = t;
        })
        .catch(() => {});
    }
    return () => {
      mounted.current = false;
      stopped.current = true;
    };
  }, [embedded, lab]);
  useEffect(() => {
    if (!lab) return;
    function restoreLocation() {
      if (!lab) return;
      const parts = location.hash.slice(1).split("-");
      const compare = parts[0] === "compare";
      const key = compare ? parts[1] : parts[0];
      const index = lab.report.layouts.findIndex((l) => l.id === key);
      if (index < 0) return;
      setRequestId(null);
      setChunk(null);
      const otherIndex = lab.report.layouts.findIndex((l) => l.id === parts[2]);
      const nextView = compare
        ? "compare"
        : parts[1] === "structure" || parts[1] === "preview"
          ? parts[1]
          : "trace";
      navigate(lab.runs[index], nextView, compare ? lab.runs[otherIndex] : undefined, false);
    }
    restoreLocation();
    window.addEventListener("hashchange", restoreLocation);
    return () => window.removeEventListener("hashchange", restoreLocation);
  }, [lab, navigate]);
  function activate(r: Run) {
    setRequestId(null);
    setChunk(null);
    navigate(r);
  }
  function selectView(nextView: AnalysisView) {
    if (lab && run) navigate(run, nextView, nextView === "compare" ? other : undefined);
    else setView(nextView);
  }
  async function measure(id: string, query: string) {
    const job = await api<{ id: string }>("/api/runs", { input_id: id, sql: query, settings });
    setJobId(job.id);
    if (stopped.current) await api(`/api/jobs/${job.id}/cancel`, {});
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 200));
      if (!mounted.current) return null;
      const state = await api<{ status: string; run?: Run; preview?: Preview; error?: string }>(
        `/api/jobs/${job.id}`,
      );
      if (state.status === "error") throw new Error(state.error);
      if (state.run) {
        const result = state.run;
        setRuns((old) => [...old.filter((r) => r.id !== result.id), result]);
        setActive(result.id);
        setRequestId(null);
        setChunk(null);
        if (state.preview) {
          const p = state.preview;
          setPreview((old) => ({ ...old, [result.id]: p }));
        }
        setJobId("");
        return result;
      }
    }
  }
  async function startRun() {
    setView("trace");
    setBusy(true);
    setError("");
    setMessage("Capturing real DuckDB HTTP requests…");
    stopped.current = false;
    try {
      await measure(inputId, sql);
      setMessage("Run finished. Select a request to inspect its range.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setJobId("");
    }
  }
  async function demo() {
    setBusy(true);
    setError("");
    stopped.current = false;
    setMessage("Generating deterministic layouts and verifying data equality…");
    try {
      const data = await api<{ inputs: InputFile[]; sql: string }>("/api/demo", {});
      setInputs(data.inputs);
      setSql(data.sql);
      const captured: Run[] = [];
      for (const id of ["demo-original", "demo-sorted"]) {
        if (stopped.current) break;
        setInputId(id);
        setMessage(
          `Measuring ${id === "demo-original" ? "original" : "sorted"} layout in a fresh worker…`,
        );
        const r = await measure(id, data.sql);
        if (r) captured.push(r);
        if (r?.sql_status !== "succeeded") break;
      }
      if (captured.length === 2) {
        setCompareId(captured[0].id);
        setView("trace");
        setMessage(
          captured[0].result_digest === captured[1].result_digest
            ? "Demo results match. Layout differences below are measured."
            : "Demo result mismatch. Inspect both artifacts.",
        );
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setJobId("");
    }
  }
  async function cancel() {
    stopped.current = true;
    setMessage("Cancelling and closing owned resources…");
    if (jobId) {
      try {
        await api(`/api/jobs/${jobId}/cancel`, {});
      } catch (e) {
        setError(String(e));
      }
    }
  }
  async function importFile(file: File) {
    setError("");
    try {
      if (file.size > MAX_BYTES) throw new Error("Artifact exceeds 32 MiB limit");
      const value = parseArtifact(await file.text());
      setRuns((old) => [...old.filter((r) => r.id !== value.id), value].slice(-20));
      activate(value);
      setMessage("Artifact opened locally. No source file or query execution required.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  function offlineExport() {
    if (!run) return;
    const standalone = !document.querySelector('script[type="module"][src]');
    const base =
      template.current ||
      (standalone ? `<!doctype html>${document.documentElement.outerHTML}` : "");
    if (!base) {
      setError(
        "Offline template is loading or unavailable. Try again, or use parquetscope viewer.",
      );
      return;
    }
    const payload = JSON.stringify(run).replaceAll("<", "\\u003c").replaceAll(">", "\\u003e");
    const doc = new DOMParser().parseFromString(base, "text/html");
    const data = doc.getElementById("parquetscope-artifact");
    if (!data) {
      setError("Offline template unavailable. Use parquetscope viewer to create it.");
      return;
    }
    data.textContent = payload;
    const root = doc.getElementById("root");
    if (root) root.replaceChildren();
    const html = `<!doctype html>${doc.documentElement.outerHTML}`;
    download(html, `${run.input.name}.viewer.html`, "text/html");
  }
  function selectChunk(s: Structure) {
    setChunk(s);
    setRequestId(null);
    selectView("trace");
  }
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <Mark />
          <span>
            ParquetScope<small>v0.1 / LOCAL INSPECTOR</small>
          </span>
        </div>
        <button type="button" className="import-button" onClick={() => fileRef.current?.click()}>
          ＋ <span>Open artifact</span>
          <kbd>JSON</kbd>
        </button>
        <input
          ref={fileRef}
          type="file"
          accept=".json,application/json"
          className="visually-hidden"
          aria-label="Import artifact"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void importFile(file);
            e.target.value = "";
          }}
        />
        <div className="sidebar-heading">
          QUERY RUNS <span>{runs.length.toString().padStart(2, "0")}</span>
        </div>
        <nav className="run-list" aria-label="Query runs">
          {runs.map((r, i) => (
            <button
              type="button"
              key={r.id}
              className={`run-item ${r.id === active ? "active" : ""}`}
              onClick={() => activate(r)}
            >
              <span className={`run-dot ${r.capture.complete ? "ok" : "warn"}`} />
              <span>
                <strong>{r.input.name}</strong>
                <small>
                  Run {String(i + 1).padStart(2, "0")} · {r.metrics.get_count} GET · {r.sql_status}
                </small>
              </span>
              <span className="run-arrow">↗</span>
            </button>
          ))}
          {!runs.length && (
            <div className="no-runs">
              Your captures will appear here.
              <br />
              Run a query or open an artifact.
            </div>
          )}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-status">
            <span className="run-dot ok" />
            {connected ? "Loopback session" : "Offline viewer"}
          </div>
          <label className="theme-control">
            Appearance
            <select
              aria-label="Appearance"
              value={theme}
              onChange={(e) => setTheme(e.target.value)}
            >
              <option value="system">System</option>
              <option value="dark">Dark</option>
              <option value="light">Light</option>
            </select>
          </label>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div>
            <span className="breadcrumb">Workspace</span>
            <span className="slash">/</span>
            <span>{run?.input.name ?? "Range inspector"}</span>
          </div>
          <div className="header-actions">
            <span className="version-tag">DuckDB 1.5.6</span>
            <button
              type="button"
              disabled={!run}
              onClick={() =>
                run && download(JSON.stringify(run, null, 2), `${run.input.name}.parquetscope.json`)
              }
            >
              ↓ Export JSON
            </button>
            <button type="button" disabled={!run} onClick={offlineExport}>
              Offline viewer ↗
            </button>
          </div>
        </header>
        <main>
          {lab && <LabIntro lab={lab} active={active} onExplore={(i) => activate(lab.runs[i])} />}
          <div className="workspace-heading">
            <h1>{lab ? "Measured run explorer" : "Range inspector"}</h1>
            <span>SELECT → HTTP → byte intervals → column chunks</span>
            {connected && demoAvailable && (
              <button type="button" disabled={busy} onClick={() => void demo()}>
                Run layout demo
              </button>
            )}
          </div>
          {error && (
            <div className="banner error" role="alert">
              {error}
              <button type="button" onClick={() => setError("")} aria-label="Dismiss error">
                ×
              </button>
            </div>
          )}
          {message && (
            <div className={`banner ${busy ? "working" : ""}`} role="status">
              {busy && <span className="spinner" />}
              {message}
              {busy && (
                <button type="button" className="cancel-button" onClick={() => void cancel()}>
                  Cancel run ■
                </button>
              )}
            </div>
          )}
          {connected && (
            <details className="panel query-panel" open={!run || undefined}>
              <summary>
                SQL workspace{" "}
                <span>{run ? run.sql : "Execute one SELECT against the input view"}</span>
              </summary>
              <div className="query-top">
                <div>
                  <span className="live-dot" /> QUERY INPUT
                </div>
                <select
                  aria-label="Input file"
                  value={inputId}
                  disabled={busy || !inputs.length}
                  onChange={(e) => setInputId(e.target.value)}
                >
                  {!inputs.length && <option value="">No file selected</option>}
                  {inputs.map((i) => (
                    <option key={i.id} value={i.id}>
                      {i.name}
                    </option>
                  ))}
                </select>
                <span className="tag">VIEW: input</span>
              </div>
              <label className="visually-hidden" htmlFor="sql">
                SQL query
              </label>
              <textarea
                id="sql"
                spellCheck={false}
                value={sql}
                onChange={(e) => setSql(e.target.value)}
                disabled={busy}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !busy && inputId) {
                    e.preventDefault();
                    void startRun();
                  }
                }}
              />
              <div className="query-footer">
                <details>
                  <summary>Execution settings</summary>
                  <div className="settings">
                    <label>
                      Deadline (s)
                      <input
                        type="number"
                        min="0.1"
                        max="300"
                        step="0.1"
                        value={settings.deadline_seconds}
                        onChange={(e) =>
                          setSettings({ ...settings, deadline_seconds: Number(e.target.value) })
                        }
                      />
                    </label>
                    <label>
                      Memory (MiB)
                      <input
                        type="number"
                        min="64"
                        max="4096"
                        value={settings.memory_limit_mb}
                        onChange={(e) =>
                          setSettings({ ...settings, memory_limit_mb: Number(e.target.value) })
                        }
                      />
                    </label>
                    <label>
                      Threads
                      <input
                        type="number"
                        min="1"
                        max="16"
                        value={settings.threads}
                        onChange={(e) =>
                          setSettings({ ...settings, threads: Number(e.target.value) })
                        }
                      />
                    </label>
                  </div>
                </details>
                <span>One SELECT · fresh worker · loopback HTTP</span>
                {!busy && (
                  <button
                    type="button"
                    className="primary"
                    disabled={!inputId || !sql.trim()}
                    onClick={() => void startRun()}
                  >
                    Run query <kbd>⌘ ↵</kbd>
                  </button>
                )}
              </div>
              {!inputs.length && (
                <p className="footnote input-help">
                  Select a file at startup with <code>parquetscope serve file.parquet</code>, or run
                  the layout demo.
                </p>
              )}
            </details>
          )}
          {run ? (
            <>
              <div className="run-heading">
                <div>
                  <h2>{run.input.name}</h2>
                  <span className={`status ${run.capture.complete ? "ok" : "warn"}`}>
                    {run.capture.complete ? "● Complete capture" : "◐ Partial capture"}
                  </span>
                  <span className="status">SQL {run.sql_status}</span>
                </div>
                <span>
                  {run.input.row_groups} row groups <span className="muted">/</span>{" "}
                  {run.input.schema_fields.length} columns <span className="muted">/</span>{" "}
                  {BigInt(run.input.rows).toLocaleString()} rows
                </span>
              </div>
              {run.sql_status === "failed" && run.errors.length > 0 && (
                <div className="banner error" role="alert">
                  {run.errors.join("; ")}
                </div>
              )}
              {!run.capture.complete && (
                <div className="banner warning" role="alert">
                  Observed subset only. {run.capture.reason}
                </div>
              )}
              <div className="metrics-strip">
                <span>
                  <b>{run.query_duration_ms?.toFixed(1) ?? "Unknown"} ms</b> SELECT
                </span>
                <span>
                  <b>{run.metrics.get_count}</b> GET / <b>{run.metrics.head_count}</b> HEAD
                </span>
                <span title={exact(run.metrics.server_written_bytes)}>
                  <b>{bytes(run.metrics.server_written_bytes)}</b> server wrote
                </span>
                <span title={exact(run.metrics.unique_coverage_bytes)}>
                  <b>{bytes(run.metrics.unique_coverage_bytes)}</b> unique coverage
                </span>
                <span title={exact(run.metrics.repeated_coverage_bytes)}>
                  <b>{bytes(run.metrics.repeated_coverage_bytes)}</b> repeated coverage
                </span>
              </div>
              <details className="captured-query">
                <summary>Captured SELECT</summary>
                <pre>{run.sql}</pre>
              </details>
              <div className="view-tabs">
                <nav aria-label="Analysis view">
                  {(["trace", "structure", "compare", "preview"] as const).map((v) => (
                    <button
                      type="button"
                      key={v}
                      aria-current={view === v ? "page" : undefined}
                      className={view === v ? "active" : ""}
                      onClick={() => selectView(v)}
                    >
                      {
                        {
                          trace: "Range lab",
                          structure: "Structure",
                          compare: "Compare runs",
                          preview: "Result preview",
                        }[v]
                      }
                    </button>
                  ))}
                </nav>
                {view === "compare" && (
                  <select
                    aria-label="Compare with run"
                    value={other?.id ?? ""}
                    onChange={(e) => {
                      const comparison = runs.find((r) => r.id === e.target.value);
                      if (lab && comparison) navigate(run, "compare", comparison);
                      else setCompareId(e.target.value);
                    }}
                  >
                    {runs
                      .filter((r) => r.id !== active)
                      .map((r) => (
                        <option key={r.id} value={r.id}>
                          {r.input.name} · {r.id.slice(0, 6)}
                        </option>
                      ))}
                  </select>
                )}
                <span className="micro">ALL OFFSETS IN BYTES · [START, END)</span>
              </div>
              {view === "trace" && (
                <RangeLab
                  key={run.id}
                  run={run}
                  requestId={requestId}
                  chunk={chunk}
                  onRequest={(id) => {
                    setRequestId(id);
                  }}
                  onChunk={selectChunk}
                />
              )}
              {view === "structure" && (
                <Structures key={run.id} run={run} selected={chunk} onSelect={selectChunk} />
              )}
              {view === "compare" &&
                (other ? (
                  <Comparison
                    a={run}
                    b={other}
                    runLabels={
                      lab
                        ? [run, other].map(
                            (r) =>
                              lab.report.layouts[lab.runs.findIndex((item) => item.id === r.id)]
                                ?.id ?? "Imported",
                          )
                        : undefined
                    }
                  />
                ) : (
                  <div className="panel empty-state">
                    <h2>A second perspective</h2>
                    <p>Run another query or open another artifact to compare measurements.</p>
                  </div>
                ))}
              {view === "preview" && (
                <section className="panel">
                  <div className="panel-heading">
                    <div>
                      <h2>Result preview</h2>
                      <p>
                        {run.result_rows ?? "Unknown"} rows fully consumed · preview limited to{" "}
                        {run.settings.preview_rows} rows / 64 KiB
                      </p>
                    </div>
                  </div>
                  {preview[run.id]?.rows.length ? (
                    <div className="table-scroll">
                      <table>
                        <thead>
                          <tr>
                            {preview[run.id].columns.map((c, i) => (
                              <th key={`${c}-${i.toString()}`}>{c}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {preview[run.id].rows.map((row, i) => (
                            <tr key={`result-${i.toString()}`}>
                              {row.map((v, j) => (
                                <td key={`cell-${j.toString()}`}>
                                  {v === null ? <i>NULL</i> : String(v)}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <p className="empty-note">
                      {run.result_rows === "0"
                        ? "The query returned no rows."
                        : "Preview is unavailable or empty. Table rows are omitted from artifacts by default."}
                    </p>
                  )}
                </section>
              )}
              <details className="measurement-notes">
                <summary>Captured SQL &amp; measurement sources</summary>
                <dl>
                  <dt>Captured SQL</dt>
                  <dd>
                    <code>{run.sql}</code>
                  </dd>
                  <dt>Requested ranges (DuckDB request headers)</dt>
                  <dd>{exact(run.metrics.requested_range_bytes)}</dd>
                  <dt>Confirmed response intervals (DuckDB response headers)</dt>
                  <dd>{exact(run.metrics.confirmed_range_bytes)}</dd>
                  <dt>Declared response body (GET Content-Length)</dt>
                  <dd>{exact(run.metrics.declared_body_bytes)}</dd>
                  <dt>Successful server stream writes</dt>
                  <dd>{exact(run.metrics.server_written_bytes)}</dd>
                  <dt>Unique coverage (union of response intervals)</dt>
                  <dd>{exact(run.metrics.unique_coverage_bytes)}</dd>
                  <dt>Repeated coverage (sum − union)</dt>
                  <dd>{exact(run.metrics.repeated_coverage_bytes)}</dd>
                  <dt>Capture scope</dt>
                  <dd>{run.capture.scope}</dd>
                  <dt>Independent server reconciliation</dt>
                  <dd>
                    {run.capture.reconciled
                      ? "Matched by response ID, method, range, status and length"
                      : "Incomplete / mismatched"}
                  </dd>
                  <dt>Input SHA-256</dt>
                  <dd>{run.input.sha256}</dd>
                </dl>
                {run.metrics.unknown_reasons.map((reason) => (
                  <p key={reason}>{reason}</p>
                ))}
                {run.limitations.map((note) => (
                  <p key={note}>{note}</p>
                ))}
              </details>
            </>
          ) : (
            <section className="panel empty-state">
              <div className="empty-graphic">
                <Mark />
                <span>SQL → HTTP → BYTES</span>
              </div>
              <h2>A query leaves a trace.</h2>
              <p>
                Capture a real DuckDB query to connect HTTP ranges with
                <br />
                row groups, column chunks and the footer.
              </p>
              <div className="empty-steps">
                <span>
                  <b>01</b> Select a local file
                </span>
                <span>
                  <b>02</b> Query the input view
                </span>
                <span>
                  <b>03</b> Inspect & compare
                </span>
              </div>
              <p className="footnote">
                Already have a capture? Open a .parquetscope.json artifact using Open artifact.
              </p>
            </section>
          )}
          {lab && <LabDetails lab={lab} />}
          <footer className="page-footer">
            <span>
              ParquetScope <b>v0.1</b>
            </span>
            <span>HTTP intervals · half-open byte offsets</span>
            <span>LOCAL / NO TELEMETRY</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
