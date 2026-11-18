//! Opens one Parquet object twice through DataFusion's Parquet opener with one shared
//! `FileMetadataCache` (apache/datafusion#23978).
//!
//! Each open builds a fresh `DataSourceExec` over the same `PartitionedFile` (same object
//! path, size and last_modified, so the same cache key) with a `ParquetSource` that uses one
//! `CachedParquetFileReaderFactory`. Executing that plan runs `ParquetMorselizer` and its open
//! state machine, the path a query takes. A `FilterExec` with the same predicate makes the
//! result exact; the scan predicate only drives row-group and page pruning.
//!
//! The process reads nothing before the first open: the fixture schema is fixed here and the
//! object size comes from the command line. Opens are sequential. Before each open it prints a
//! `begin` event and waits for `go` on stdin; after it, an `end` event and waits for `next`.
//! The runner uses these boundaries to attribute server observations to an open.

use std::error::Error;
use std::io::{BufRead, Write};
use std::sync::Arc;

use datafusion::arrow::array::{Array, Int64Array};
use datafusion::arrow::datatypes::{DataType, Field, Schema, SchemaRef};
use datafusion::common::config::TableParquetOptions;
use datafusion::datasource::listing::PartitionedFile;
use datafusion::datasource::physical_plan::parquet::CachedParquetFileReaderFactory;
use datafusion::datasource::physical_plan::{FileScanConfigBuilder, ParquetSource};
use datafusion::datasource::source::DataSourceExec;
use datafusion::execution::cache::DefaultFilesMetadataCache;
use datafusion::execution::cache::cache_manager::FileMetadataCache;
use datafusion::execution::object_store::ObjectStoreUrl;
use datafusion::logical_expr::Operator;
use datafusion::physical_expr::PhysicalExpr;
use datafusion::physical_expr::expressions::{binary, col, is_not_null, lit};
use datafusion::physical_plan::filter::FilterExec;
use datafusion::physical_plan::{ExecutionPlan, collect};
use datafusion::prelude::SessionContext;
use object_store::ClientOptions;
use object_store::http::HttpBuilder;
use object_store::path::Path;

type Res<T> = Result<T, Box<dyn Error>>;

struct Args {
    base_url: String,
    path: String,
    size: u64,
    column: String,
    kind: String,
    opens: usize,
}

fn parse_args() -> Res<Args> {
    let mut base_url = None;
    let mut path = None;
    let mut size = None;
    let mut predicate = None;
    let mut opens = 2;
    let mut it = std::env::args().skip(1);
    while let Some(flag) = it.next() {
        let value = it.next().ok_or(format!("missing value for {flag}"))?;
        match flag.as_str() {
            "--base-url" => base_url = Some(value),
            "--path" => path = Some(value),
            "--size" => size = Some(value.parse()?),
            "--predicate" => predicate = Some(value),
            "--opens" => opens = value.parse()?,
            _ => return Err(format!("unknown flag {flag}").into()),
        }
    }
    let predicate: String = predicate.ok_or("--predicate is required")?;
    let (column, kind) = predicate
        .split_once(':')
        .ok_or("--predicate must be <column>:is_not_null or <column>:ge_zero")?;
    Ok(Args {
        base_url: base_url.ok_or("--base-url is required")?,
        path: path.ok_or("--path is required")?,
        size: size.ok_or("--size is required")?,
        column: column.to_string(),
        kind: kind.to_string(),
        opens,
    })
}

/// The fixture schema written by scripts/datafusion_page_index_lab.py (all nullable Int64).
fn fixture_schema() -> SchemaRef {
    Arc::new(Schema::new(vec![
        Field::new("id", DataType::Int64, true),
        Field::new("fk", DataType::Int64, true),
        Field::new("val", DataType::Int64, true),
    ]))
}

fn emit(line: String) -> Res<()> {
    let mut out = std::io::stdout().lock();
    writeln!(out, "{line}")?;
    out.flush()?;
    Ok(())
}

fn wait_for(token: &str) -> Res<()> {
    let mut line = String::new();
    std::io::stdin().lock().read_line(&mut line)?;
    if line.trim() != token {
        return Err(format!("expected {token:?} on stdin, got {:?}", line.trim()).into());
    }
    Ok(())
}

fn json_str(value: &str) -> String {
    let mut s = String::from("\"");
    for c in value.chars() {
        match c {
            '"' => s.push_str("\\\""),
            '\\' => s.push_str("\\\\"),
            c if (c as u32) < 0x20 => s.push_str(&format!("\\u{:04x}", c as u32)),
            c => s.push(c),
        }
    }
    s.push('"');
    s
}

/// Whether the shared cache entry for the object carries the page index.
fn cache_state(cache: &Arc<dyn FileMetadataCache>, location: &Path) -> String {
    match cache.get(location) {
        None => "\"absent\"".into(),
        // `extra_info` is DataFusion's own description of a cached Parquet entry
        // (`page_index` = column index and offset index both present).
        Some(entry) => match entry.file_metadata.extra_info().get("page_index") {
            Some(value) => format!("{{\"page_index\":{}}}", json_str(value)),
            None => "\"unknown\"".into(),
        },
    }
}

#[tokio::main]
async fn main() -> Res<()> {
    let args = parse_args()?;
    let store = Arc::new(
        HttpBuilder::new()
            .with_url(&args.base_url)
            .with_client_options(ClientOptions::new().with_allow_http(true))
            .build()?,
    );
    let store_url = ObjectStoreUrl::parse(&args.base_url)?;
    let ctx = SessionContext::new();
    ctx.register_object_store(store_url.as_ref(), Arc::clone(&store) as _);

    // One fresh cache per process; one factory shared by every open.
    let cache: Arc<dyn FileMetadataCache> = Arc::new(DefaultFilesMetadataCache::new(64 << 20));
    let factory = Arc::new(CachedParquetFileReaderFactory::new(
        Arc::clone(&store) as _,
        Arc::clone(&cache),
    ));

    let schema = fixture_schema();
    let column = col(&args.column, &schema)?;
    let (predicate, predicate_sql): (Arc<dyn PhysicalExpr>, String) = match args.kind.as_str() {
        "is_not_null" => (is_not_null(column)?, format!("{} IS NOT NULL", args.column)),
        "ge_zero" => (
            binary(column, Operator::GtEq, lit(0i64), &schema)?,
            format!("{} >= 0", args.column),
        ),
        other => return Err(format!("unknown predicate kind {other}").into()),
    };
    // DataFusion's defaults for listing tables (page index on, 512 KiB footer hint).
    let options = TableParquetOptions::default();
    let hint = options.global.metadata_size_hint;
    let file = PartitionedFile::new(args.path.clone(), args.size);
    let location = file.object_meta.location.clone();

    emit(format!(
        "{{\"event\":\"ready\",\"datafusion_version\":{},\"predicate\":{},\
         \"enable_page_index\":{},\"pushdown_filters\":{},\"metadata_size_hint\":{},\
         \"cache_key\":{},\"last_modified\":{}}}",
        json_str(datafusion::DATAFUSION_VERSION),
        json_str(&predicate_sql),
        options.global.enable_page_index,
        options.global.pushdown_filters,
        hint.map_or("null".into(), |h| h.to_string()),
        json_str(location.as_ref()),
        json_str(&file.object_meta.last_modified.to_rfc3339()),
    ))?;

    for open in 1..=args.opens {
        emit(format!("{{\"event\":\"begin\",\"open\":{open}}}"))?;
        wait_for("go")?;

        let mut source = ParquetSource::new(Arc::clone(&schema))
            .with_table_parquet_options(options.clone())
            .with_predicate(Arc::clone(&predicate))
            .with_parquet_file_reader_factory(Arc::clone(&factory) as _);
        if let Some(h) = hint {
            source = source.with_metadata_size_hint(h);
        }
        let config = FileScanConfigBuilder::new(store_url.clone(), Arc::new(source))
            .with_file(file.clone())
            .build();
        let scan: Arc<dyn ExecutionPlan> = DataSourceExec::from_data_source(config);
        let plan = Arc::new(FilterExec::try_new(
            Arc::clone(&predicate),
            Arc::clone(&scan),
        )?);
        let batches = collect(plan, ctx.task_ctx()).await?;

        let (mut rows, mut sum_id, mut sum_val, mut mix) = (0u64, 0i128, 0i128, 0i128);
        for batch in &batches {
            let ids = batch
                .column(0)
                .as_any()
                .downcast_ref::<Int64Array>()
                .ok_or("id")?;
            let vals = batch
                .column(2)
                .as_any()
                .downcast_ref::<Int64Array>()
                .ok_or("val")?;
            for i in 0..batch.num_rows() {
                rows += 1;
                if ids.is_valid(i) {
                    sum_id += ids.value(i) as i128;
                }
                if vals.is_valid(i) {
                    sum_val += vals.value(i) as i128;
                }
                if ids.is_valid(i) && vals.is_valid(i) {
                    mix += (ids.value(i) as i128 * 31 + vals.value(i) as i128) % 1_000_003;
                }
            }
        }

        let mut metrics = Vec::new();
        if let Some(set) = scan.metrics() {
            let mut set = set.aggregate_by_name();
            set = set.sorted_for_display();
            for metric in set.iter() {
                let value = metric.value();
                // Display keeps compound metrics (e.g. matched / fully matched row groups).
                metrics.push(format!(
                    "{}:{}",
                    json_str(value.name()),
                    json_str(&value.to_string())
                ));
            }
        }
        emit(format!(
            "{{\"event\":\"end\",\"open\":{open},\"result\":{{\"rows\":{rows},\
             \"sum_id\":\"{sum_id}\",\"sum_val\":\"{sum_val}\",\"mix\":\"{mix}\"}},\
             \"cache_after\":{},\"metrics\":{{{}}}}}",
            cache_state(&cache, &location),
            metrics.join(","),
        ))?;
        wait_for("next")?;
    }
    emit("{\"event\":\"done\"}".into())?;
    Ok(())
}
