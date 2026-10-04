use std::io::Cursor;

use polars::prelude::*;

fn main() -> PolarsResult<()> {
    let csv = Cursor::new(b"group,value\na,1\nb,2\na,3\nb,4\n".to_vec());
    let frame = CsvReadOptions::default()
        .with_has_header(true)
        .into_reader_with_file_handle(csv)
        .finish()?;
    let mut result = frame
        .lazy()
        .group_by([col("group")])
        .agg([col("value").sum()])
        .sort(["group"], Default::default())
        .collect()?;
    assert_eq!(result.height(), 2);
    assert_eq!(result.column("value")?.i64()?.sum(), Some(10));
    let mut bytes = Vec::new();
    ParquetWriter::new(&mut bytes).finish(&mut result)?;
    let restored = ParquetReader::new(Cursor::new(bytes)).finish()?;
    assert!(restored.equals(&result));
    println!("polars: csv+lazy+parquet groups=2 sum=10");
    Ok(())
}
