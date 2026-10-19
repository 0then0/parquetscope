import { useEffect, useMemo, useRef, useState } from "react";
import { bytes, exact, intersects, type Run, rangeText, type Span, type Structure } from "./model";
import { position, tick } from "./rangeGeometry";

const PAGE = 12;
export function RangeLab({
  run,
  requestId,
  chunk,
  onRequest,
  onChunk,
}: {
  run: Run;
  requestId: string | null;
  chunk: Structure | null;
  onRequest: (id: string) => void;
  onChunk: (s: Structure) => void;
}) {
  const [page, setPage] = useState(0);
  const [related, setRelated] = useState(false);
  const [zoom, setZoom] = useState<Span | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const [width, setWidth] = useState(800);
  const initial = run.observations.find(
    (o) =>
      o.method === "GET" &&
      o.response_span &&
      BigInt(o.response_span.start) < BigInt(run.input.size) / 2n,
  );
  const selected =
    chunk && !requestId ? null : (run.observations.find((o) => o.id === requestId) ?? initial);
  const span = selected ? selected.response_span : (chunk?.span ?? null);
  const domain = zoom ?? { start: "0", end: run.input.size };
  const records = useMemo(
    () => [...run.observations].sort((a, b) => a.start_ms - b.start_ms),
    [run],
  );
  const list =
    related && chunk ? records.filter((o) => intersects(o.response_span, chunk.span)) : records;
  const maxTime = records.reduce((max, o) => Math.max(max, o.start_ms + o.duration_ms), 1);
  const chunks = useMemo(
    () => run.input.structures.filter((s) => intersects(s.span, selected?.response_span ?? null)),
    [run, selected],
  );
  const server = run.server_observations.find((o) => o.id === selected?.id);
  const left = 66,
    right = 20,
    top = 106,
    bottom = 340;
  const x = (offset: string) => left + position(offset, domain) * (width - left - right);
  const y = (ms: number) => top + (ms / maxTime) * (bottom - top);
  useEffect(() => {
    const node = canvas.current;
    if (!node) return;
    const observer = new ResizeObserver(([entry]) =>
      setWidth(Math.max(280, entry.contentRect.width)),
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const node = canvas.current;
    const ctx = node?.getContext("2d");
    if (!node || !ctx) return;
    const draw = () => {
      const divisions = width < 640 ? 2 : 4;
      const ratio = window.devicePixelRatio || 1;
      node.width = width * ratio;
      node.height = 380 * ratio;
      ctx.scale(ratio, ratio);
      const styles = getComputedStyle(node);
      const muted = styles.getPropertyValue("--muted");
      const line = styles.getPropertyValue("--line");
      const accent = styles.getPropertyValue("--accent");
      ctx.font = "11px ui-monospace, monospace";
      ctx.fillStyle = muted;
      ctx.fillText("FILE OFFSET · BYTES", left, 18);
      ctx.fillText("LAYOUT", 3, 58);
      ctx.fillText("START ms", 3, 92);
      ctx.save();
      ctx.beginPath();
      ctx.rect(left, 40, width - left - right, 305);
      ctx.clip();
      for (const s of run.input.structures) {
        if (!intersects(s.span, domain)) continue;
        ctx.fillStyle =
          s.id === chunk?.id || intersects(s.span, selected?.response_span ?? null)
            ? accent
            : s.kind === "footer"
              ? "#b88a4a"
              : "#8293a855";
        ctx.fillRect(x(s.span.start), 44, Math.max(1, x(s.span.end) - x(s.span.start) - 0.5), 17);
      }
      for (let i = 0; i <= divisions; i++) {
        ctx.strokeStyle = line;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(x(tick(domain, i, divisions)), 70);
        ctx.lineTo(x(tick(domain, i, divisions)), bottom);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(left, y((maxTime * i) / divisions));
        ctx.lineTo(width - right, y((maxTime * i) / divisions));
        ctx.stroke();
      }
      for (const o of records) {
        if (o.method !== "GET" || !o.response_span || !intersects(o.response_span, domain))
          continue;
        ctx.strokeStyle = o.id === selected?.id ? accent : "#7189ad";
        ctx.lineWidth = o.id === selected?.id ? 5 : 2;
        ctx.beginPath();
        ctx.moveTo(x(o.response_span.start), y(o.start_ms));
        ctx.lineTo(Math.max(x(o.response_span.start) + 2, x(o.response_span.end)), y(o.start_ms));
        ctx.stroke();
      }
      if (span && intersects(span, domain)) {
        ctx.strokeStyle = accent;
        ctx.lineWidth = 1;
        ctx.setLineDash([3, 4]);
        for (const edge of [span.start, span.end]) {
          ctx.beginPath();
          ctx.moveTo(x(edge), 40);
          ctx.lineTo(x(edge), bottom);
          ctx.stroke();
        }
        ctx.setLineDash([]);
      }
      if (selected?.response_span && intersects(selected.response_span, domain)) {
        ctx.strokeStyle = accent;
        ctx.lineWidth = 5;
        ctx.beginPath();
        ctx.moveTo(x(selected.response_span.start), y(selected.start_ms));
        ctx.lineTo(
          Math.max(x(selected.response_span.start) + 2, x(selected.response_span.end)),
          y(selected.start_ms),
        );
        ctx.stroke();
      }
      ctx.restore();
      ctx.fillStyle = muted;
      for (let i = 0; i <= divisions; i++) {
        ctx.textAlign = i === divisions ? "right" : i === 0 ? "left" : "center";
        const label = tick(domain, i, divisions);
        ctx.fillText(label.length > 12 ? bytes(label) : label, x(label), 79);
        ctx.textAlign = "right";
        ctx.fillText(
          ((maxTime * i) / divisions).toFixed(1),
          left - 10,
          y((maxTime * i) / divisions) + 4,
        );
      }
      ctx.textAlign = "left";
      ctx.fillText("Each mark = one GET response interval at its request start time", left, 369);
    };
    draw();
    const observer = new MutationObserver(draw);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme"],
    });
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    media.addEventListener("change", draw);
    return () => {
      observer.disconnect();
      media.removeEventListener("change", draw);
    };
  });
  function chooseChunk(value: Structure) {
    setPage(0);
    onChunk(value);
  }
  function choose(id: string) {
    onRequest(id);
    const index = list.findIndex((o) => o.id === id);
    if (index >= 0) setPage(Math.floor(index / PAGE));
  }
  return (
    <section className="range-lab">
      <div className="lab-toolbar">
        <div>
          <h2>Range lab</h2>
          <p>HTTP response intervals × request start time</p>
        </div>
        <div className="zoom-controls">
          <button
            type="button"
            className="primary zoom-button"
            disabled={!span}
            onClick={() => setZoom(span)}
          >
            ⊕ Zoom to selected range
          </button>
          <button type="button" disabled={!zoom} onClick={() => setZoom(null)}>
            Whole file
          </button>
        </div>
      </div>
      <div className="lab-grid">
        <div className="plot-panel">
          <div className="plot-caption">
            <span>{zoom ? "ZOOMED RANGE" : "WHOLE FILE"}</span>
            <code data-testid="zoom-domain">{rangeText(domain)}</code>
            <span>{records.length} HTTP observations</span>
          </div>
          <canvas
            ref={canvas}
            className="range-canvas"
            role="img"
            aria-label="Byte ranges by request start time. Use the request table for exact ranges and keyboard selection."
            onClick={(e) => {
              const rect = e.currentTarget.getBoundingClientRect();
              const px = e.clientX - rect.left,
                py = e.clientY - rect.top;
              if (py >= 40 && py <= 65) {
                const found = run.input.structures.find(
                  (s) => px >= x(s.span.start) && px <= x(s.span.end),
                );
                if (found) chooseChunk(found);
                return;
              }
              const nearest = records
                .filter((o) => o.method === "GET" && intersects(o.response_span, domain))
                .reduce<{ id: string; distance: number } | null>((best, o) => {
                  if (!o.response_span) return best;
                  const dx = Math.max(
                    x(o.response_span.start) - px,
                    0,
                    px - x(o.response_span.end),
                  );
                  const distance = Math.hypot(dx, y(o.start_ms) - py);
                  return !best || distance < best.distance ? { id: o.id, distance } : best;
                }, null);
              if (nearest && nearest.distance < 22) choose(nearest.id);
            }}
          />
          <div className="plot-legend">
            <span>
              <i /> GET response interval
            </span>
            <span>
              <i className="selected-key" /> Selection / intersecting layout
            </span>
            <span>HEAD has no response body · mark height does not encode duration</span>
          </div>
        </div>
        <aside className="details">
          <div className="inspector-label">LINKED INSPECTOR</div>
          <h3>{selected ? "Request details" : chunk ? "Chunk details" : "Select a request"}</h3>
          {selected ? (
            <>
              <div className="request-title">
                <b>{selected.method}</b>
                <span>{selected.status}</span>
                <code>{selected.id.slice(0, 12)}</code>
              </div>
              <dl>
                <dt>Request start / duration</dt>
                <dd>
                  {selected.start_ms.toFixed(3)} / {selected.duration_ms.toFixed(3)} ms
                </dd>
                <dt>Requested Range header</dt>
                <dd>
                  <code>{selected.requested_range ?? "None"}</code>
                </dd>
                <dt>Confirmed response · [start, end)</dt>
                <dd>
                  <code>{rangeText(selected.response_span)}</code>
                </dd>
                <dt>Declared body / server wrote</dt>
                <dd>
                  {exact(selected.declared_body_bytes)} /{" "}
                  {server ? exact(server.written_body_bytes) : "Unknown"}
                </dd>
                <dt>Independent server stream</dt>
                <dd>{server ? (server.complete ? "Completed" : "Interrupted") : "Not matched"}</dd>
              </dl>
              <h4>
                Intersecting file regions <span>{chunks.length}</span>
              </h4>
              <div className="chunk-links">
                {chunks.slice(0, 30).map((s) => (
                  <button type="button" key={s.id} onClick={() => chooseChunk(s)}>
                    {s.label}
                    <small>{rangeText(s.span)}</small>
                  </button>
                ))}
              </div>
              {chunks.length > 30 && (
                <p>First 30 shown. All regions are available under Structure.</p>
              )}
              <details className="http-headers">
                <summary>HTTP request &amp; response headers</summary>
                <pre>
                  {JSON.stringify(
                    { request: selected.request_headers, response: selected.response_headers },
                    null,
                    2,
                  )}
                </pre>
              </details>
            </>
          ) : chunk ? (
            <>
              <strong>{chunk.label}</strong>
              <dl>
                <dt>Byte interval</dt>
                <dd>
                  <code>{rangeText(chunk.span)}</code>
                </dd>
                <dt>Compression</dt>
                <dd>{chunk.compression ?? "—"}</dd>
                <dt>Related requests</dt>
                <dd>{records.filter((o) => intersects(o.response_span, chunk.span)).length}</dd>
              </dl>
            </>
          ) : (
            <p>Select a row to connect its HTTP response to Parquet column chunks.</p>
          )}
        </aside>
      </div>
      <div className="ledger-heading">
        <h3>
          Request ledger <span>{list.length}</span>
        </h3>
        <label>
          <input
            type="checkbox"
            checked={related}
            disabled={!chunk}
            onChange={(e) => {
              setRelated(e.target.checked);
              setPage(0);
            }}
          />{" "}
          Related requests only
        </label>
        <span>Ordered by request start · compact</span>
      </div>
      <div className="table-scroll">
        <table className="request-ledger">
          <thead>
            <tr>
              <th>Request</th>
              <th>Start ms</th>
              <th>Duration ms</th>
              <th>Status</th>
              <th>Response interval [start, end)</th>
              <th>Declared body</th>
              <th>Intersects</th>
            </tr>
          </thead>
          <tbody>
            {list.slice(page * PAGE, (page + 1) * PAGE).map((o) => (
              <tr
                key={o.id}
                className={`request-row ${selected?.id === o.id ? "selected" : ""} ${intersects(o.response_span, chunk?.span ?? null) ? "related" : ""}`}
              >
                <td>
                  <button
                    type="button"
                    aria-label={`Request ${o.method} ${o.id}`}
                    onClick={() => choose(o.id)}
                  >
                    {o.method} <small>{o.id.slice(0, 8)}</small>
                  </button>
                </td>
                <td>{o.start_ms.toFixed(3)}</td>
                <td>{o.duration_ms.toFixed(3)}</td>
                <td>{o.status}</td>
                <td>
                  <code>{rangeText(o.response_span)}</code>
                </td>
                <td>{bytes(o.declared_body_bytes)}</td>
                <td>
                  {run.input.structures.find(
                    (s) => s.kind === "column_chunk" && intersects(s.span, o.response_span),
                  )?.label ?? (o.method === "HEAD" ? "No body" : "Metadata / other")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="pagination">
        <span>
          {list.length ? page * PAGE + 1 : 0}–{Math.min((page + 1) * PAGE, list.length)} of{" "}
          {list.length}
        </span>
        <button
          type="button"
          aria-label="Previous page"
          disabled={!page}
          onClick={() => setPage(page - 1)}
        >
          ←
        </button>
        <button
          type="button"
          aria-label="Next page"
          disabled={(page + 1) * PAGE >= list.length}
          onClick={() => setPage(page + 1)}
        >
          →
        </button>
      </div>
    </section>
  );
}
