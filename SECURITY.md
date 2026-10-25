# Security policy

## Supported versions

Security fixes target the latest released ParquetScope version, currently **0.1.0**, and the `main` branch. Older releases have no separate security backport commitment. Use the pinned DuckDB version, **1.5.6**; other engine versions are outside the validated capture contract.

## Report a vulnerability

Do not disclose exploit details, sensitive captures, credentials, or personal data in a public issue or pull request.

Private vulnerability reporting is not currently enabled for this repository. Use the [private contact request form](https://github.com/0then0/parquetscope/issues/new?template=private_contact.yml) and select **Security**, with no vulnerability details. The maintainer will arrange a private channel before you share the report. There is no guaranteed response time.

A private report should include the affected version and platform, impact, reproduction steps, and a minimal synthetic example where possible. Coordinate publication with the maintainer so a fix can be prepared before technical details become public.

## Security boundaries

ParquetScope is a local diagnostic tool for trusted Parquet files and SQL. Its server binds to `127.0.0.1` and has no authentication layer. Do not expose it through a public interface, reverse proxy, or shared remote service.

Queries run in a separate worker with bounded execution settings. This is not an OS sandbox for hostile SQL or files. The configured DuckDB memory limit is an engine setting, not a process-wide memory cap. Install httpfs explicitly with `parquetscope setup` before capture; extension installation requires external network access.

Saved artifacts are validated before opening, but validation does not establish authentic provenance. A capture retains SQL, column names, HTTP headers, a file hash, and layout metadata. Result previews and the original source path are omitted. Review artifacts and standalone HTML before sharing; use synthetic data for public reproductions.

See the [artifact reference](docs/artifacts.md) and [measurement contract](docs/measurements.md) for validation rules and limitations.
