# Databricks notebook source
# MAGIC %md
# MAGIC # App Analytics — Spark → Delta (Trip Planner)
# MAGIC
# MAGIC The capstone's **"analytics inside a Delta table about your app"** piece.
# MAGIC
# MAGIC The Flask app logs usage events (`analytics.py`) into the Lakebase
# MAGIC `app_events` table — the same reliable write path every other app write
# MAGIC uses. This notebook reads those events with **Spark (JDBC)**, mirrors them
# MAGIC into a **Delta** bronze table, and computes usage analytics into a **Delta
# MAGIC gold** table (`workspace.trip_planner.app_analytics`) with charts you can pin
# MAGIC to a dashboard.
# MAGIC
# MAGIC ```
# MAGIC  Flask app --(log_event)--> Lakebase.app_events
# MAGIC                                   │  Spark JDBC read
# MAGIC                                   ▼
# MAGIC              app_events_delta (Delta bronze)  ──►  app_analytics (Delta gold) + charts
# MAGIC ```
# MAGIC
# MAGIC > Uses Spark's **JDBC reader** (JVM `org.postgresql.Driver`) — not `psycopg2`
# MAGIC > — so it runs on Serverless (the psycopg2/Spark-JDBC-*writer* limits don't
# MAGIC > apply to a JDBC read).

# COMMAND ----------

import base64
from urllib.parse import urlparse

from pyspark.sql import functions as F

CATALOG, SCHEMA = "workspace", "trip_planner"
BRONZE = f"{CATALOG}.{SCHEMA}.app_events_delta"
GOLD = f"{CATALOG}.{SCHEMA}.app_analytics"

spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read `app_events` from Lakebase (Spark JDBC)
# MAGIC
# MAGIC Resolves the same base64-encoded `database/lakebase-url` secret the app uses,
# MAGIC and turns it into JDBC connection options.

# COMMAND ----------

raw = base64.b64decode(dbutils.secrets.get(scope="database", key="lakebase-url")).decode("utf-8")
u = urlparse(raw)
jdbc_url = f"jdbc:postgresql://{u.hostname}:{u.port or 5432}{u.path}?sslmode=require"

events = (
    spark.read.format("jdbc")
    .option("url", jdbc_url)
    .option("dbtable", "app_events")
    .option("user", u.username)
    .option("password", u.password)
    .option("driver", "org.postgresql.Driver")
    .load()
)
print(f"{events.count()} events read from Lakebase")
events.orderBy(F.col("created_at").desc()).show(10, truncate=40)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Mirror into a Delta bronze table
# MAGIC A governed Delta copy of the raw events (queryable, time-travelable).

# COMMAND ----------

events.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(BRONZE)
print(f"Wrote {spark.table(BRONZE).count()} rows to {BRONZE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Analytics about the app
# MAGIC Event mix, daily activity, an engagement funnel (how many trips reach each
# MAGIC stage), and the most active users.

# COMMAND ----------

by_type = events.groupBy("event_type").count().orderBy(F.col("count").desc())
display(by_type)  # bar chart: event_type vs count

# COMMAND ----------

by_day = (events.withColumn("day", F.to_date("created_at"))
          .groupBy("day").count().orderBy("day"))
display(by_day)  # line chart: activity over time

# COMMAND ----------

def _trips_with(evt):
    return events.filter(F.col("event_type") == evt).select("trip_id").distinct().count()

funnel = spark.createDataFrame(
    [
        ("1_created", _trips_with("trip_created")),
        ("2_destination_added", _trips_with("destination_added")),
        ("3_itinerary_generated", _trips_with("itinerary_generated")),
        ("4_rescheduled", _trips_with("reschedule")),
        ("5_packing_built", _trips_with("packing_built")),
    ],
    ["stage", "trips"],
)
display(funnel)  # engagement funnel

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4. Write the Delta gold analytics table
# MAGIC One tidy summary (one row per metric) a dashboard or the grader can query.

# COMMAND ----------

summary = (
    by_type.select(
        F.lit("events_by_type").alias("metric"),
        F.col("event_type").alias("dimension"),
        F.col("count").alias("value"))
    .unionByName(funnel.select(
        F.lit("funnel").alias("metric"),
        F.col("stage").alias("dimension"),
        F.col("trips").alias("value")))
    .unionByName(spark.createDataFrame(
        [("totals", "total_events", events.count()),
         ("totals", "distinct_trips", events.select("trip_id").distinct().count())],
        ["metric", "dimension", "value"]))
    .withColumn("computed_at", F.current_timestamp())
)

summary.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(GOLD)
print(f"Wrote {summary.count()} rows to {GOLD}")
display(spark.table(GOLD))
