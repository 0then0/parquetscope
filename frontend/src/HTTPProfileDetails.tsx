import { useState } from "react";
import { exact, httpProfileText, type Run } from "./model";
import { Pagination } from "./Pagination";

const PAGE = 20;
const ms = (value: number | null | undefined) => (value == null ? "—" : value.toFixed(3));

/** Includes server-only requests when a stopped worker could not save its engine log. */
export function HTTPProfileDetails({ run }: { run: Run }) {
  const [page, setPage] = useState(0);
  const profile = run.http_profile;
  return (
    <details className="captured-query http-profile-details">
      <summary>HTTP profile · {httpProfileText(profile?.effective)}</summary>
      {profile && (
        <>
          <dl>
            <dt>Requested</dt>
            <dd>{httpProfileText(profile.requested)}</dd>
            <dt>Effective</dt>
            <dd>{httpProfileText(profile.effective)}</dd>
            <dt>Limiter semantics</dt>
            <dd>{profile.semantics}</dd>
            <dt>Body burst allowance</dt>
            <dd>{exact(profile.burst_bytes)}</dd>
            <dt>Maximum write chunk</dt>
            <dd>{exact(String(profile.write_chunk_bytes))}</dd>
          </dl>
          <p className="footnote">
            Delay starts after route, identity and range checks, before sending headers. Body budget
            is reserved before each server write. Successful writes enter the response stream; they
            do not confirm receipt by DuckDB. This models a local HTTP server, not transport RTT or
            cloud performance.
          </p>
          <details>
            <summary>Server request timings · {run.server_observations.length} records</summary>
            <p className="footnote">
              All times are milliseconds on the server's monotonic clock. Delay, budget wait and
              write-call durations are measured per request. First-to-last write includes
              intervening budget waits. Parallel waits overlap and cannot be summed into query wall
              time. Server-only records remain available when the engine log is incomplete.
            </p>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Request / file</th>
                    <th>Method / status</th>
                    <th>Handling start / duration</th>
                    <th>Delay start / actual wait</th>
                    <th>Headers sent</th>
                    <th>Budget wait</th>
                    <th>First / last body write</th>
                    <th>Write-call time</th>
                    <th>Reserved / written bytes</th>
                    <th>Termination</th>
                  </tr>
                </thead>
                <tbody>
                  {run.server_observations.slice(page * PAGE, (page + 1) * PAGE).map((s) => (
                    <tr key={s.id}>
                      <td>
                        <code title={s.id}>{s.id.slice(0, 12)}</code>
                        <br />
                        {s.file_id}
                      </td>
                      <td>
                        {s.method} / {s.status}
                      </td>
                      <td>
                        {ms(s.start_ms)} / {ms(s.duration_ms)}
                      </td>
                      <td>
                        {ms(s.profile_delay_started_ms)} / {ms(s.profile_delay_ms)}
                      </td>
                      <td>{ms(s.headers_sent_ms)}</td>
                      <td>{ms(s.body_budget_wait_ms)}</td>
                      <td>
                        {ms(s.body_write_started_ms)} / {ms(s.body_write_finished_ms)}
                      </td>
                      <td>{ms(s.body_write_ms)}</td>
                      <td>
                        {s.reserved_body_bytes} / {s.written_body_bytes}
                      </td>
                      <td title={s.error ?? undefined}>{s.termination}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination
              page={page}
              total={run.server_observations.length}
              size={PAGE}
              onChange={setPage}
            />
          </details>
        </>
      )}
    </details>
  );
}
