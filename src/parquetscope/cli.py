import argparse
import json
from pathlib import Path

from .engines import SUPPORTED_ENGINES


def main():
    parser = argparse.ArgumentParser(
        prog="parquetscope", description="Inspect real Parquet HTTP reads"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser(
        "setup",
        help="Install the pinned DuckDB httpfs extension, or prepare an isolated engine "
        "(requires internet)",
    )
    setup.add_argument(
        "--engine",
        choices=SUPPORTED_ENGINES,
        help="Prepare an isolated environment with this DuckDB version and its httpfs",
    )
    serve = sub.add_parser(
        "serve", help="Open local files and/or saved artifacts in the browser UI"
    )
    serve.add_argument("files", nargs="*", type=Path)
    serve.add_argument(
        "--manifest",
        action="append",
        default=[],
        type=Path,
        help="Explicit file-set manifest (repeatable); see docs/manifest.md",
    )
    serve.add_argument("--artifact", action="append", default=[], type=Path)
    serve.add_argument("--demo", action="store_true")
    serve.add_argument("--port", type=int, default=8400)
    demo = sub.add_parser("demo", help="Generate two layouts, capture and verify both queries")
    demo.add_argument("--output", type=Path, default=Path("demo-output"))
    capture = sub.add_parser("capture", help="Capture one SELECT as an artifact")
    capture.add_argument("file", nargs="?", type=Path, help="One Parquet file (v4 artifact)")
    capture.add_argument("--manifest", type=Path, help="A file-set manifest (v4 artifact)")
    capture.add_argument("--sql", required=True)
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--deadline", type=float, default=30)
    capture.add_argument(
        "--response-delay-ms",
        type=int,
        default=0,
        help="Wait before each prepared HEAD/GET response's headers (0..60000 ms; default 0)",
    )
    capture.add_argument(
        "--body-bytes-per-second",
        type=int,
        help="Shared body write limit across this capture (1..1000000000 bytes/s; "
        "default unrestricted). Low rates may reach --deadline",
    )
    capture.add_argument(
        "--engine",
        choices=SUPPORTED_ENGINES,
        help="Run in the prepared isolated environment of this DuckDB version "
        "(--manifest only; prepare it with `setup --engine`)",
    )
    capture.add_argument(
        "--prefetch",
        choices=("default", "on", "off"),
        default="default",
        help="'off' sets disable_parquet_prefetching=true, 'on' sets it to false, "
        "'default' leaves the engine's value (--manifest only)",
    )
    viewer = sub.add_parser("viewer", help="Create a self-contained offline HTML viewer")
    viewer.add_argument("artifact", type=Path)
    viewer.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "setup":
            if args.engine:
                from .engines import engine_root, prepare_engine

                facts = prepare_engine(args.engine)
                print(
                    f"DuckDB {facts['duckdb_version']} with httpfs {facts['httpfs_version']} "
                    f"ready: {engine_root(args.engine)}"
                )
            else:
                from .capture import setup_httpfs

                try:
                    directory = setup_httpfs()
                except Exception as error:
                    raise RuntimeError(f"httpfs setup failed: {error}") from error
                print(f"DuckDB 1.5.6 httpfs ready: {directory}")
        elif args.command == "serve":
            import tempfile

            import uvicorn

            from .app import create_app
            from .manifest import load_manifest
            from .models import load_artifact_file

            artifacts = [load_artifact_file(p) for p in args.artifact]
            for path in args.files:
                if not path.is_file():
                    raise ValueError(f"Not a file: {path}")
            manifests = [load_manifest(p) for p in args.manifest]
            with tempfile.TemporaryDirectory(prefix="parquetscope-demo-") as temp:
                app = create_app(
                    args.files, artifacts, Path(temp) if args.demo else None, manifests
                )
                print(f"ParquetScope: http://127.0.0.1:{args.port}")
                uvicorn.run(app, host="127.0.0.1", port=args.port)
        elif args.command == "demo":
            from .demo import run_demo

            _, report = run_demo(args.output)
            print(json.dumps(report, indent=2))
        elif args.command == "capture":
            from .http_profile import HTTPProfile
            from .models import Settings, save_artifact
            from .models_v3 import Settings3

            if (args.file is None) == (args.manifest is None):
                raise ValueError("Give exactly one input: a Parquet file or --manifest")
            if args.file is not None and (args.engine or args.prefetch != "default"):
                raise ValueError("--engine and --prefetch need a file set (--manifest)")
            settings = Settings3(
                threads=4,
                memory_limit_mb=512,
                deadline_seconds=args.deadline,
                preview_rows=30,
                disable_parquet_prefetching={"default": None, "on": False, "off": True}[
                    args.prefetch
                ],
            )
            profile = HTTPProfile(
                response_delay_ms=args.response_delay_ms,
                body_bytes_per_second=args.body_bytes_per_second,
            )
            if args.manifest:
                from .capture import capture_set
                from .manifest import load_manifest

                spec = load_manifest(args.manifest)
                protected = [args.manifest, *(e.source for e in spec.entries)]
                if args.output.exists() and any(args.output.samefile(p) for p in protected):
                    raise ValueError("Output must not refer to the manifest or an input file")
                run, _ = capture_set(
                    spec, args.sql, settings, engine=args.engine, http_profile=profile
                )
            else:
                if args.output.exists() and args.output.samefile(args.file):
                    raise ValueError("Output must not refer to the input Parquet file")

                from .capture import capture

                run, _ = capture(
                    args.file,
                    args.sql,
                    Settings(**settings.model_dump(exclude={"disable_parquet_prefetching"})),
                    http_profile=profile,
                )
            save_artifact(run, args.output)
            print(f"SQL {run.sql_status}; capture complete={run.capture.complete}; {args.output}")
            if run.sql_status != "succeeded" or run.errors or not run.capture.complete:
                raise ValueError("; ".join(run.errors) or run.capture.reason)
        elif args.command == "viewer":
            # Import no execution modules and make no network requests.
            from .models import load_artifact_file
            from .offline import viewer_html

            run = load_artifact_file(args.artifact)
            args.output.write_text(viewer_html(run))
            print(args.output.resolve())
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"parquetscope: {error}\n")


if __name__ == "__main__":
    main()
