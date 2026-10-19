import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        prog="parquetscope", description="Inspect real Parquet HTTP reads"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("setup", help="Install the pinned DuckDB httpfs extension (requires internet)")
    serve = sub.add_parser(
        "serve", help="Open local files and/or saved artifacts in the browser UI"
    )
    serve.add_argument("files", nargs="*", type=Path)
    serve.add_argument("--artifact", action="append", default=[], type=Path)
    serve.add_argument("--demo", action="store_true")
    serve.add_argument("--port", type=int, default=8400)
    demo = sub.add_parser("demo", help="Generate two layouts, capture and verify both queries")
    demo.add_argument("--output", type=Path, default=Path("demo-output"))
    capture = sub.add_parser("capture", help="Capture one SELECT as an artifact")
    capture.add_argument("file", type=Path)
    capture.add_argument("--sql", required=True)
    capture.add_argument("--output", required=True, type=Path)
    capture.add_argument("--deadline", type=float, default=30)
    viewer = sub.add_parser("viewer", help="Create a self-contained offline HTML viewer")
    viewer.add_argument("artifact", type=Path)
    viewer.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "setup":
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
            from .models import load_artifact_file

            artifacts = [load_artifact_file(p) for p in args.artifact]
            for path in args.files:
                if not path.is_file():
                    raise ValueError(f"Not a file: {path}")
            with tempfile.TemporaryDirectory(prefix="parquetscope-demo-") as temp:
                app = create_app(args.files, artifacts, Path(temp) if args.demo else None)
                print(f"ParquetScope: http://127.0.0.1:{args.port}")
                uvicorn.run(app, host="127.0.0.1", port=args.port)
        elif args.command == "demo":
            from .demo import run_demo

            _, report = run_demo(args.output)
            print(json.dumps(report, indent=2))
        elif args.command == "capture":
            if args.output.exists() and args.output.samefile(args.file):
                raise ValueError("Output must not refer to the input Parquet file")

            from .capture import capture
            from .models import Settings, save_artifact

            run, _ = capture(
                args.file,
                args.sql,
                Settings(
                    threads=4,
                    memory_limit_mb=512,
                    deadline_seconds=args.deadline,
                    preview_rows=30,
                ),
            )
            save_artifact(run, args.output)
            print(f"SQL {run.sql_status}; capture complete={run.capture.complete}; {args.output}")
            if run.errors or not run.capture.complete:
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
