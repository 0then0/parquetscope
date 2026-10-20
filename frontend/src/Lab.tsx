import { bytes, exact, parseArtifact, type Run } from "./model";

export type LabLayout = {
  id: string;
  label: string;
  selected_repetition: number;
  viewer: string;
  repetitions: { repetition: number; capture: string }[];
};
export type LabReport = {
  schema_version: string;
  status: string;
  sql: string;
  layouts: LabLayout[];
  versions: { python: string; duckdb: string; pyarrow: string };
  source: { page: string; url: string; sha256: string };
  limitations: string[];
  previous_attempts?: { report: string; reason: string }[];
};
export type LabData = { report: LabReport; runs: Run[] };

async function jsonFile(path: string) {
  const response = await fetch(new URL(path, location.href));
  if (!response.ok) throw new Error(`Cannot load lab file: ${path} (${response.status})`);
  return response.text();
}
export async function loadLab(): Promise<LabData> {
  const report: LabReport = JSON.parse(await jsonFile("./report.json"));
  if (
    report.schema_version !== "1" ||
    report.status !== "complete" ||
    report.layouts.map((l) => l.id).join() !== "A,B,C" ||
    report.layouts.some((l) => l.selected_repetition !== 1 || l.repetitions.length !== 3)
  )
    throw new Error("Invalid or incomplete real-data lab report");
  const runs = await Promise.all(
    report.layouts.map(async (layout) => {
      const selected = layout.repetitions.find((r) => r.repetition === 1);
      if (!selected) throw new Error("Missing repetition 1");
      const run = parseArtifact(await jsonFile(selected.capture));
      if (run.sql !== report.sql || run.sql_status !== "succeeded" || !run.capture.complete)
        throw new Error("Lab capture does not match the successful workload");
      return run;
    }),
  );
  return { report, runs };
}

export function LabIntro({
  lab,
  active,
  onExplore,
}: {
  lab: LabData;
  active: string;
  onExplore: (index: number) => void;
}) {
  const b = BigInt(lab.runs[1].metrics.server_written_bytes);
  const c = BigInt(lab.runs[2].metrics.server_written_bytes);
  const reduction = b > 0n ? (Number(((b - c) * 10000n) / b) / 100).toFixed(2) : "unknown";
  return (
    <section className="lab-intro" aria-label="Experiment overview">
      <p className="lab-kicker">REAL DATA / NYC TLC YELLOW TAXI / JANUARY 2025</p>
      <h1>How Parquet layout changes HTTP reads</h1>
      <p className="lab-question">
        Does a smaller Parquet file mean fewer requests or fewer bytes read? Explore the same SELECT
        over 3,475,226 rows in three measured layouts.
      </p>
      <p className="lab-static">
        Premeasured captures, repetition 1 of 3 for each layout. This static site executes no SQL.
        Every successful repetition is available below. No source Parquet is loaded here.
      </p>
      <div className="lab-cards">
        {lab.report.layouts.map((layout, i) => {
          const run = lab.runs[i];
          return (
            <article
              key={layout.id}
              className={`panel lab-card ${active === run.id ? "selected" : ""}`}
            >
              <span className="lab-kicker">LAYOUT {layout.id} / REPETITION 1</span>
              <h2>{layout.label}</h2>
              <dl>
                <dt>File size · {run.input.row_groups} row groups</dt>
                <dd title={exact(run.input.size)}>{bytes(run.input.size)}</dd>
                <dt>Observed requests</dt>
                <dd>
                  {run.metrics.get_count} GET + {run.metrics.head_count} HEAD
                </dd>
                <dt>Server-written body</dt>
                <dd title={exact(run.metrics.server_written_bytes)}>
                  {bytes(run.metrics.server_written_bytes)}
                </dd>
              </dl>
              <details>
                <summary>Exact byte counts</summary>
                <p>
                  File: {exact(run.input.size)}
                  <br />
                  Server wrote: {exact(run.metrics.server_written_bytes)}
                </p>
              </details>
              <a
                href={`#${layout.id}`}
                className="lab-explore"
                aria-current={active === run.id ? "true" : undefined}
                onClick={(event) => {
                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
                  event.preventDefault();
                  onExplore(i);
                }}
              >
                Explore {layout.id} →
              </a>
            </article>
          );
        })}
      </div>
      <p className="lab-finding">
        B is{" "}
        {BigInt(lab.runs[1].input.size) < BigInt(lab.runs[0].input.size)
          ? "smaller"
          : "at least as large"}{" "}
        than A and has{" "}
        {lab.runs[1].metrics.get_count > lab.runs[0].metrics.get_count ? "more" : "no more"} GETs in
        these captures. C writes {reduction}% fewer body bytes than B, using B’s server-written
        bytes as the denominator, repetition 1 on both sides. Sorting changes the opportunity to
        skip row groups; this does not establish a speedup.
      </p>
      <div className="lab-links">
        <a href="#compare-B-C">Compare B ↔ C</a>
        <a href="#compare-A-B">Compare A ↔ B</a>
        <button
          type="button"
          className="text-button"
          onClick={() => {
            const section = document.getElementById("lab-method");
            section?.scrollIntoView();
            section?.focus({ preventScroll: true });
          }}
        >
          Method &amp; downloads
        </button>
      </div>
    </section>
  );
}

export function LabDetails({ lab }: { lab: LabData }) {
  return (
    <section className="panel lab-details" id="lab-method" tabIndex={-1}>
      <h2>Method, evidence &amp; downloads</h2>
      <p>
        A is the unmodified <a href={lab.report.source.page}>official NYC TLC source</a>. B and C
        use DuckDB {lab.report.versions.duckdb}, Zstandard level 3, Parquet V1 and ROW_GROUP_SIZE
        122880. B preserves original order; C uses ORDER BY PULocationID. A/B also differ in writer
        and encoding. B/C share writer settings.
      </p>
      <p>
        Preparation uses one thread, 1 GB engine memory and temporary spill. All columns and
        duplicate rows are checked using bidirectional EXCEPT ALL for A/B and A/C; persisted logical
        schemas and original order in B are checked separately. Preparation is outside the captures.
        Arrow string/large_string offset widths are reported separately from Parquet UTF8 / SQL
        VARCHAR logical types.
      </p>
      <pre>{lab.report.sql}</pre>
      <p>
        Exact result in every run: n = 142826, fare = 8038349.75. DECIMAL(18,2) is part of the
        workload. Every capture has a fresh worker, 4 threads, a 512 MiB DuckDB memory limit, a
        60-second deadline and disabled external file cache. Binding plus one fully consumed SELECT
        is captured and reconciled against independent server observations. Preparation and equality
        checks run separately from measurement.
      </p>
      <p>
        Python {lab.report.versions.python} · PyArrow {lab.report.versions.pyarrow}. Input SHA-256:
      </p>
      <code className="lab-hash">{lab.report.source.sha256}</code>
      <details>
        <summary>Measurement boundaries &amp; limitations</summary>
        <ul>
          {lab.report.limitations.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
        <p>
          Summary cards and comparison reuse validated artifact metrics. Exact response intervals
          and their union remain available in Range lab, comparison and captures. HEAD has no body.
          Durations are observations, not controlled timing benchmarks.
        </p>
      </details>
      {lab.report.previous_attempts?.length ? (
        <details>
          <summary>Execution history</summary>
          <p>Excluded attempts are preserved separately from the nine successful repetitions.</p>
          {lab.report.previous_attempts.map((attempt) => (
            <p key={attempt.report}>
              {attempt.reason}{" "}
              <a href={`./${attempt.report}`} download>
                Download failed-attempt report
              </a>
            </p>
          ))}
        </details>
      ) : null}
      <h3>All repetitions, unchanged native artifacts</h3>
      <div className="lab-downloads">
        {lab.report.layouts.map((layout) => (
          <div key={layout.id}>
            <h3>
              {layout.id}. {layout.label}
            </h3>
            {layout.repetitions.map((r) => (
              <a key={r.capture} href={`./${r.capture}`} download>
                Capture {r.repetition}
              </a>
            ))}
            <a href={`./${layout.viewer}`} download>
              Standalone viewer · repetition 1
            </a>
          </div>
        ))}
      </div>
      <p className="lab-links">
        <a href="./report.json" download>
          Machine-readable report
        </a>
        <a href="./manifest.json" download>
          File manifest
        </a>
        <a href="./SHA256SUMS.txt" download>
          SHA-256 checksums
        </a>
        <a href="./README.md">Case study &amp; full reproduction commands</a>
      </p>
      <h3>Reproduce from a clean checkout</h3>
      <p>
        Uses the published v0.1.0 wheel in a separate environment. Full commands and wheel hash are
        in the case study. Setup downloads httpfs explicitly before measurement.
      </p>
      <pre>{`# From the checkout; after installing/verifying the release wheel:
REPO="$PWD"
cd /tmp
PARQUETSCOPE_HOME=/tmp/tlc-runtime /tmp/tlc-env/bin/python \\
  "$REPO/scripts/real_data_lab.py" \\
  --wheel /tmp/parquetscope-0.1.0-py3-none-any.whl \\
  --work-dir /tmp/tlc-work --output /tmp/tlc-evidence`}</pre>
      <p>
        <a href="https://github.com/0then0/parquetscope">ParquetScope project &amp; source</a> ·
        Explore sorting and row-group pruning through the reads observed for this workload.
        Downloads include SQL, column names, headers and aggregate results; source trip rows are
        excluded.
      </p>
    </section>
  );
}
