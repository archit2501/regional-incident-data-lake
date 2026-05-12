"""Glue job: anonymize coordinates (round to 3 decimal places) and convert
raw incident JSON to Parquet.

Skew-resilient version. The previous implementation OOM'd because one
intersection shipped 10 M pings in a 24-hour window and the aggregator's
shuffle pinned all of them to a single executor. We do not raise Worker
Type or Max Capacity; instead we apply three orthogonal fixes:

  1. Spark Adaptive Query Execution (AQE) with runtime skew-join splitting.
  2. Two-stage salted aggregation for grouped operations.
  3. Time-only Parquet partitioning + a pre-write repartition on a finer
     spatial hash so the hot intersection spreads across multiple writers.

Job arguments (passed by the IngestionStateMachine):
    --source_bucket      e.g. incident-landing-prod
    --source_prefix      e.g. staging/agency-a/2026-05-12/feed-001/
    --target_bucket      e.g. incident-curated-prod
    --target_prefix      e.g. curated/
    --gps_precision      e.g. 3
"""
import sys

from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import DataFrame, functions as F

ARGS = getResolvedOptions(
    sys.argv,
    [
        "JOB_NAME",
        "source_bucket",
        "source_prefix",
        "target_bucket",
        "target_prefix",
        "gps_precision",
    ],
)

sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(ARGS["JOB_NAME"], ARGS)

# ---------------------------------------------------------------------------
# 1. Adaptive Query Execution (AQE) — runtime skew-join + partition coalesce
# ---------------------------------------------------------------------------
spark.conf.set("spark.sql.adaptive.enabled", "true")
spark.conf.set("spark.sql.adaptive.skewJoin.enabled", "true")
spark.conf.set("spark.sql.adaptive.skewJoin.skewedPartitionFactor", "5")
spark.conf.set(
    "spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes",
    str(256 * 1024 * 1024),
)
spark.conf.set("spark.sql.adaptive.coalescePartitions.enabled", "true")

SOURCE = f"s3://{ARGS['source_bucket']}/{ARGS['source_prefix']}"
TARGET = f"s3://{ARGS['target_bucket']}/{ARGS['target_prefix']}"
GPS_PRECISION = int(ARGS["gps_precision"])
SALT_BUCKETS = 64  # spreads any single hot key across 64 executor partitions


# ---------------------------------------------------------------------------
# 2. Anonymize: round lat/lon and stamp partitioning columns
# ---------------------------------------------------------------------------
def anonymize(df: DataFrame) -> DataFrame:
    return (
        df.withColumn("lat", F.round(F.col("lat"), GPS_PRECISION))
        .withColumn("lon", F.round(F.col("lon"), GPS_PRECISION))
        .withColumn("event_date", F.to_date("event_ts"))
        .withColumn("event_hour", F.hour("event_ts"))
    )


# ---------------------------------------------------------------------------
# 3. Two-stage salted aggregation for the skewed groupBy(intersection_id)
#    Stage 1: aggregate by (intersection_id, salt). Each (intersection, salt)
#             bucket is small, so SALT_BUCKETS executors share the load.
#             Emit sum + count (NOT avg) so the second stage can compute a
#             correctly-weighted centroid — avg(avg(x)) ≠ avg(x) when group
#             sizes differ, which they do under salting.
#    Stage 2: strip the salt and re-aggregate per intersection — only
#             SALT_BUCKETS partial rows per hot key to shuffle.
# ---------------------------------------------------------------------------
def aggregate_salted(df: DataFrame) -> DataFrame:
    salted = df.withColumn(
        "_salt",
        # rand(seed=...) is evaluated per row (the seed only fixes the stream),
        # so this produces a uniform 0..SALT_BUCKETS-1 salt per record.
        (F.rand(seed=1729) * SALT_BUCKETS).cast("int"),
    )
    stage1 = salted.groupBy("intersection_id", "_salt").agg(
        F.count("*").alias("partial_pings"),
        F.sum("lat").alias("partial_sum_lat"),
        F.sum("lon").alias("partial_sum_lon"),
    )
    return stage1.groupBy("intersection_id").agg(
        F.sum("partial_pings").alias("ping_count"),
        (F.sum("partial_sum_lat") / F.sum("partial_pings")).alias("centroid_lat"),
        (F.sum("partial_sum_lon") / F.sum("partial_pings")).alias("centroid_lon"),
    )


# ---------------------------------------------------------------------------
# 4. Read → anonymize → write Parquet
#    - Partition output by (region, event_date, event_hour) only.
#    - Pre-shuffle on a finer composite hash so the hot intersection spreads
#      across multiple writer tasks instead of one fan-out target.
# ---------------------------------------------------------------------------
raw = spark.read.json(SOURCE)
clean = anonymize(raw)

# Pre-write repartition: include a per-row random salt INSTEAD of
# intersection_id. Hashing (region, date, hour, intersection_id) as a tuple
# would still funnel all 10 M hot-intersection rows into a single partition,
# because the tuple hash is deterministic per key. A random salt breaks the
# hot key across multiple writer tasks so a single (region, date, hour)
# directory is produced by ~400/(regions*hours) writers, not one.
write_ready = clean.repartition(
    400,
    F.col("region"),
    F.col("event_date"),
    F.col("event_hour"),
    (F.rand() * SALT_BUCKETS).cast("int"),
)

(
    write_ready.write.mode("append")
    .option("maxRecordsPerFile", 500_000)
    .partitionBy("region", "event_date", "event_hour")
    .parquet(TARGET)
)

# Aggregated heatmap source — runs the salted aggregator so the same 10 M
# pings on one intersection do not recreate the OOM here.
heatmap = aggregate_salted(clean)
heatmap.write.mode("overwrite").parquet(f"{TARGET.rstrip('/')}_heatmap")

job.commit()
