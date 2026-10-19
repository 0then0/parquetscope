import hashlib
import os
from pathlib import Path

import pyarrow.parquet as pq

from .intervals import complement
from .models import Input, Structure, span


def identity(stat: os.stat_result):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def read_metadata(path: Path) -> tuple[Input, tuple]:
    """Preparation only: no HTTP access, no statistics/min/max or table rows exported."""
    before = identity(path.stat())
    digest = hashlib.sha256()
    with path.open("rb") as source:
        if source.read(4) != b"PAR1":
            raise ValueError("Expected a standard, unencrypted Parquet file (PAR1)")
        source.seek(0)
        while block := source.read(1024 * 1024):
            digest.update(block)
        source.seek(-8, 2)
        trailer = source.read(8)
        if trailer[4:] != b"PAR1":
            raise ValueError("Invalid or encrypted Parquet footer")
        size = before[2]
        footer_start = size - 8 - int.from_bytes(trailer[:4], "little")
        if footer_start < 4:
            raise ValueError("Invalid footer length")
        source.seek(0)
        metadata = pq.ParquetFile(source).metadata
    structures = [
        Structure(id="header", kind="header", label="PAR1 header", span=span(0, 4)),
        Structure(
            id="footer",
            kind="footer",
            label="Footer + length + magic",
            span=span(footer_start, size),
        ),
    ]
    for rg_index in range(metadata.num_row_groups):
        rg = metadata.row_group(rg_index)
        for col_index in range(rg.num_columns):
            col = rg.column(col_index)
            if col.file_path:
                raise ValueError("External column chunks are outside single-file scope")
            # file_offset can be 0. The compressed chunk includes dictionary pages.
            offsets = [o for o in (col.dictionary_page_offset, col.data_page_offset) if o and o > 0]
            if not offsets or col.total_compressed_size <= 0:
                continue
            start = min(offsets)
            end = start + col.total_compressed_size
            if start < 4 or end > footer_start:
                raise ValueError("Column chunk outside data region")
            structures.append(
                Structure(
                    id=f"rg{rg_index}-c{col_index}",
                    kind="column_chunk",
                    label=f"RG {rg_index} · {col.path_in_schema}",
                    span=span(start, end),
                    row_group=rg_index,
                    column=col.path_in_schema,
                    rows=str(rg.num_rows),
                    dictionary_offset=str(col.dictionary_page_offset)
                    if col.has_dictionary_page
                    else None,
                    data_offset=str(col.data_page_offset),
                    compression=col.compression,
                )
            )
    known = [s.span.pair() for s in structures]
    for index, (start, end) in enumerate(complement(known, size)):
        structures.append(
            Structure(
                id=f"unknown-{index}",
                kind="unclassified",
                label="Unclassified",
                span=span(start, end),
            )
        )
    if identity(path.stat()) != before:
        raise ValueError("Input changed during preparation")
    if len(structures) > 100000:
        raise ValueError("Input exceeds v0.1 metadata limit (100,000 structures)")
    return Input(
        name=path.name,
        sha256=digest.hexdigest(),
        size=str(size),
        rows=str(metadata.num_rows),
        row_groups=metadata.num_row_groups,
        schema_fields=[metadata.schema.column(i).path for i in range(metadata.num_columns)],
        structures=structures,
    ), before
