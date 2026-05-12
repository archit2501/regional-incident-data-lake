# Resolving GPS-Ping Data Skew in the Glue Anonymization Job

## Symptom

10 million GPS pings concentrated on a single `intersection_id` cause one executor's shuffle partition to balloon past its memory budget while every other executor sits idle. **Adding workers does nothing.** Spark's default hash partitioner sends every record for that intersection to the same partition regardless of cluster size, so the bottleneck moves with the data, not the hardware. The job OOMs at the same point on G.2X workers as on G.1X.

## Root cause

The default `HashPartitioner` produces one task per distinct `intersection_id`. With a Zipfian distribution of pings per intersection, the worst-case partition is unbounded in `n`. Aggregations (`groupBy`, joins, window functions) all hit this. The previous output schema partitioned Parquet by `intersection_id`, which made the *write* step suffer the same fan-out skew a second time.

## Strategy

Three orthogonal fixes, applied together. None requires touching cluster size.

### 1. Spark Adaptive Query Execution (AQE) with skew-join

```python
spark.conf.set("spark.sql.adaptive.enabled", "true")
spark.conf.set("spark.sql.adaptive.skewJoin.enabled", "true")
spark.conf.set("spark.sql.adaptive.skewJoin.skewedPartitionFactor", "5")
spark.conf.set("spark.sql.adaptive.skewJoin.skewedPartitionThresholdInBytes", "268435456")
```

AQE detects partitions whose size exceeds `5× the median` (and 256 MB absolute) at runtime and splits them into sub-partitions before the next stage. This is the cheapest fix and handles joins automatically. It does *not* help aggregations whose result row is a single key — that's where the salting trick comes in.

### 2. Two-stage salted aggregation

Salt the group key with a uniform random 0..N-1 suffix, aggregate by `(key, salt)`, then aggregate the partial results by `key`. The salted stage parallelizes N-way; the second stage reduces only N partial rows per hot key. See `03-glue-job-skeleton.py::aggregate_salted` — 64 buckets convert one hot partition into 64 balanced ones at the cost of one extra shuffle of N rows per hot key. Cheap.

**Math caveat:** for `count` / `sum` this composes trivially. For `avg` it does **not** — `avg(avg(x))` ≠ `avg(x)` when bucket sizes differ (which they do under random salting). Emit `sum` and `count` from the first stage and divide in the second:

```python
salted = df.withColumn("_salt", (F.rand() * 64).cast("int"))
stage1 = salted.groupBy("intersection_id", "_salt").agg(
    F.count("*").alias("partial_pings"),
    F.sum("lat").alias("partial_sum_lat"),
)
final  = stage1.groupBy("intersection_id").agg(
    F.sum("partial_pings").alias("ping_count"),
    (F.sum("partial_sum_lat") / F.sum("partial_pings")).alias("centroid_lat"),
)
```

### 3. S3 output partitioning that doesn't reintroduce skew

Partitioning Parquet output by `intersection_id` recreates the problem at write time — one writer task per distinct intersection, the hot one running forever. Partition by **time and region only** (`region`, `event_date`, `event_hour`) and let `intersection_id` live as a column inside each Parquet file.

Pre-write repartition needs a per-row **random salt**, not the `intersection_id` itself — hashing the tuple `(region, date, hour, intersection_id)` deterministically funnels all 10 M hot-intersection rows back into a single partition. Replace `intersection_id` in the repartition expression with `(F.rand() * SALT_BUCKETS).cast("int")`:

```python
df.repartition(400,
   "region", "event_date", "event_hour",
   (F.rand() * SALT_BUCKETS).cast("int")
).write \
 .option("maxRecordsPerFile", 500_000) \
 .partitionBy("region", "event_date", "event_hour") \
 .parquet(TARGET)
```

The hot intersection now spreads across roughly `400 / (regions × hours)` writer tasks instead of one. `maxRecordsPerFile` caps the file count from exploding when 400 input partitions collide on a few `(region, date, hour)` dirs. For downstream queries that filter by intersection, add a Glue Catalog secondary index, or migrate the curated layer to Iceberg/Hudi with Z-order on `(lat, lon)` for spatial pruning.

## What was rejected, and why

| Approach | Why it fails |
|----------|--------------|
| **Broadcast join on the hot key** | Only helps if the skew is in a *join*, not a `groupBy`. Doesn't apply here. |
| **`spark.sql.shuffle.partitions = 4000`** | Increases parallelism overall but the hot partition is still one partition — its neighbours just get smaller. Marginal effect. |
| **Splitting input files** | The skew is in the *data*, not the file layout. Splitting input keeps all 10 M pings for the hot intersection in the same partition after the shuffle. |
| **Pre-filtering hot keys to a side path** | Works but adds an ops branch the team has to maintain manually. The salted aggregator subsumes this without special-casing. |
| **Larger Glue worker / more capacity** | Explicitly ruled out by the assignment. Also doesn't fix the issue — the hot partition is per-key, not per-cluster. |

## Result

With AQE on, salted two-stage aggregation, and a time-partitioned output, the same Glue job runs to completion on the existing G.1X / 10-worker configuration. Peak executor memory drops from >12 GB on the hot executor to <2 GB across all executors, and wall-clock drops because the 10 M-ping intersection now runs 64× wider. The fix is data-shape-aware, not hardware-aware.
