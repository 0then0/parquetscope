import { useCallback, useEffect, useRef, useState } from "react";
import projectIconSvg from "../../docs/icon.svg?raw";
import { Comparison } from "./Comparison";
import { FileOverview } from "./FileOverview";
import { type LabData, LabDetails, LabIntro } from "./Lab";
import {
  bytes,
  exact,
  type FileEntry,
  fileTitle,
  intersects,
  MAX_BYTES,
  parseArtifact,
  parseArtifactValue,
  type Run,
  type Settings,
  type Structure,
} from "./model";
import { Pagination } from "./Pagination";
import { RangeLab } from "./RangeLab";

type InputFile = { id: string; name: string; kind: "file" | "set"; file_count: number };
type Preview = { columns: string[]; rows: (string | boolean | null)[][] };
type Session = { inputs: InputFile[]; runs: unknown[]; demo_available: boolean };
type AnalysisView = "files" | "trace" | "structure" | "compare" | "preview";
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
function defaultFile(run: Run): FileEntry {
  const withGet = run.file_metrics.find((m) => m.observed_access === "get");
  return run.files.find((f) => f.id === withGet?.file_id) ?? run.files[0];
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
function Structures({
  run,
  file,
  selected,
  onSelect,
}: {
  run: Run;
  file: FileEntry;
  selected: Structure | null;
  onSelect: (s: Structure) => void;
}) {
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const observations = run.byFile.get(file.id)?.observations ?? [];
  const list = file.structures.filter((s) =>
    `${s.label} ${s.kind}`.toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <section className="panel">
      <div className="panel-heading">
        <div>
          <h2>Physical structure</h2>
          <p>
            {fileTitle(file)} · {file.row_groups} row groups · {file.schema_fields.length} columns ·
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
                <td>{observations.filter((o) => intersects(o.response_span, s.span)).length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pagination page={page} total={list.length} size={50} onChange={setPage} />
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
  const [view, setView] = useState<AnalysisView>(
    embedded?.schema_version === "2" ? "files" : "trace",
  );
  const [requestId, setRequestId] = useState<string | null>(null);
  const [chunk, setChunk] = useState<Structure | null>(null);
  const [fileId, setFileId] = useState("");
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
  const file = run ? (run.files.find((f) => f.id === fileId) ?? defaultFile(run)) : undefined;
  const other =
    runs.find((r) => r.id === compareId && r.id !== active) ?? runs.find((r) => r.id !== active);
  const navigate = useCallback(
    (
      next: Run,
      nextView: AnalysisView = next.schema_version === "2" ? "files" : "trace",
      comparison?: Run,
      updateHistory = true,
    ) => {
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
          const stored = s.runs.flatMap((value) => {
            try {
              return [parseArtifactValue(value)];
            } catch {
              return [];
            }
          });
          setRuns(stored);
          setActive(stored[0]?.id ?? "");
          setSql(stored[0]?.sql ?? DEFAULT_SQL);
          if (stored[0]?.schema_version === "2") setView("files");
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
    setFileId("");
    navigate(r);
  }
  function inspectFile(id: string) {
    setFileId(id);
    setChunk(null);
    setRequestId(null);
    selectView("trace");
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
      const state = await api<{
        status: string;
        run?: unknown;
        preview?: Preview;
        error?: string;
      }>(`/api/jobs/${job.id}`);
      if (state.status === "error") throw new Error(state.error);
      if (state.run) {
        const result = parseArtifactValue(state.run);
        setRuns((old) => [...old.filter((r) => r.id !== result.id), result]);
        setActive(result.id);
        setRequestId(null);
        setChunk(null);
        setFileId("");
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
      const result = await measure(inputId, sql);
      if (result?.schema_version === "2") setView("files");
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
    const payload = JSON.stringify(run.artifact)
      .replaceAll("<", "\\u003c")
      .replaceAll(">", "\\u003e");
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
    download(html, `${run.label}.viewer.html`, "text/html");
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
            ParquetScope<small>v0.2 / LOCAL INSPECTOR</small>
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
                <strong>{r.label}</strong>
                <small>
                  Run {String(i + 1).padStart(2, "0")}
                  {r.files.length > 1 ? ` · ${r.files.length} files` : ""} · {r.metrics.get_count}{" "}
                  GET · {r.sql_status}
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
            <span>{run?.label ?? "Range inspector"}</span>
          </div>
          <div className="header-actions">
            <span className="version-tag">DuckDB 1.5.6</span>
            <button
              type="button"
              disabled={!run}
              onClick={() =>
                run &&
                download(JSON.stringify(run.artifact, null, 2), `${run.label}.parquetscope.json`)
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
                      {i.kind === "set" ? `${i.name} · ${i.file_count} files` : i.name}
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
                  <h2>{run.label}</h2>
                  <span className={`status ${run.capture.complete ? "ok" : "warn"}`}>
                    {run.capture.complete ? "● Complete capture" : "◐ Partial capture"}
                  </span>
                  <span className="status">SQL {run.sql_status}</span>
                </div>
                <span>
                  {run.files.length > 1
                    ? `${run.files.length} files`
                    : `${run.files[0].row_groups} row groups`}{" "}
                  <span className="muted">/</span> {run.files[0].schema_fields.length} columns{" "}
                  <span className="muted">/</span>{" "}
                  {run.files.reduce((n, f) => n + BigInt(f.rows), 0n).toLocaleString()} rows
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
                  {(run.schema_version === "2"
                    ? (["files", "trace", "structure", "compare", "preview"] as const)
                    : (["trace", "structure", "compare", "preview"] as const)
                  ).map((v) => (
                    <button
                      type="button"
                      key={v}
                      aria-current={view === v ? "page" : undefined}
                      className={view === v ? "active" : ""}
                      onClick={() => selectView(v)}
                    >
                      {
                        {
                          files: "Files",
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
                          {r.label} · {r.id.slice(0, 6)}
                        </option>
                      ))}
                  </select>
                )}
                <span className="micro">ALL OFFSETS IN BYTES · [START, END)</span>
              </div>
              {run.files.length > 1 && file && (view === "trace" || view === "structure") && (
                <div className="file-picker">
                  <label>
                    File
                    <select
                      aria-label="Inspected file"
                      value={file.id}
                      onChange={(e) => {
                        setFileId(e.target.value);
                        setChunk(null);
                        setRequestId(null);
                      }}
                    >
                      {run.files.map((f) => (
                        <option key={f.id} value={f.id}>
                          {fileTitle(f)}
                        </option>
                      ))}
                    </select>
                  </label>
                  <span className="micro">
                    Byte offsets and intervals belong to this file only.
                  </span>
                </div>
              )}
              {view === "files" && file && (
                <FileOverview run={run} selectedId={file.id} onInspect={inspectFile} />
              )}
              {view === "trace" && file && (
                <RangeLab
                  key={`${run.id}-${file.id}`}
                  run={run}
                  file={file}
                  requestId={requestId}
                  chunk={chunk}
                  onRequest={(id) => {
                    setRequestId(id);
                  }}
                  onChunk={selectChunk}
                />
              )}
              {view === "structure" && file && (
                <Structures
                  key={`${run.id}-${file.id}`}
                  run={run}
                  file={file}
                  selected={chunk}
                  onSelect={selectChunk}
                />
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
                  {run.files.length === 1 ? (
                    <>
                      <dt>Input SHA-256</dt>
                      <dd>{run.files[0].sha256}</dd>
                    </>
                  ) : (
                    <>
                      <dt>File set</dt>
                      <dd>
                        {run.files.length} files with their own SHA-256 under Files. Paths and
                        partition values are exported and may be sensitive.
                      </dd>
                    </>
                  )}
                  <dt>Read options</dt>
                  <dd>
                    {run.read
                      ? `hive_partitioning=${run.read.hive_partitioning}, union_by_name=${run.read.union_by_name}`
                      : "Not recorded (v1 artifact)"}
                  </dd>
                  <dt>Engine</dt>
                  <dd>
                    {run.engine
                      ? `DuckDB ${run.duckdb_version}, httpfs ${run.engine.httpfs_version ?? "unknown"}`
                      : "httpfs version and effective settings not recorded (v1 artifact)"}
                  </dd>
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
              ParquetScope <b>v0.2</b>
            </span>
            <span>HTTP intervals · half-open byte offsets</span>
            <span>LOCAL / NO TELEMETRY</span>
          </footer>
        </main>
      </div>
    </div>
  );
}
