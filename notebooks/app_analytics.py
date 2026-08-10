# Databricks notebook source
# MAGIC %md
# MAGIC # App Analytics — Spark over a Delta Table (Trip Planner)
# MAGIC
# MAGIC The capstone's **"analytics inside a Delta table about your app"** piece.
# MAGIC
# MAGIC The Flask app logs usage events (`analytics.py`) into a Unity Catalog
# MAGIC **Delta** table `workspace.trip_planner.app_events` via the SQL Statement
# MAGIC Execution API. This notebook reads that Delta table with **Spark**, computes
# MAGIC usage analytics, and writes a governed **Delta gold** table
# MAGIC `workspace.trip_planner.app_analytics` — plus charts you can pin to a
# MAGIC dashboard.
# MAGIC
# MAGIC ```
# MAGIC  Flask app --(log_event, Statement Execution)--> app_events (Delta bronze)
# MAGIC                                                        │  Spark
# MAGIC                                                        ▼
# MAGIC                                          app_analytics (Delta gold) + charts
# MAGIC ```

# COMMAND ----------

from pyspark.sql import functions as F

CATALOG = "workspace"
SCHEMA = "trip_planner"
EVENTS = f"{CATALOG}.{SCHEMA}.app_events"
GOLD = f"{CATALOG}.{SCHEMA}.app_analytics"

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1. Read the raw events (Delta bronze)

# COMMAND ----------

if not spark.catalog.tableExists(EVENTS):
    print(f"{EVENTS} doesn't exist yet — use the app (create a trip, generate an "
          "itinerary, chat with the agent) so it logs events, then re-run.")
    dbutils.notebook.exit("no events table yet")

events = spark.table(EVENTS)
print(f"{events.count()} events logged")
events.orderBy(F.col("created_at").desc()).show(10, truncate=40)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2. Analytics about the app
# MAGIC
# MAGIC Event mix, daily activity, engagement funnel (how many trips reach each
# MAGIC stage), and the most active users — the kind of product analytics you'd put
# MAGIC on a dashboard.

# COMMAND ----------

by_type = (events.groupBy("event_type").count().orderBy(F.col("count").desc()))
print("Events by type:")
by_type.show(truncate=False)
display(by_type)  # bar chart: event_type vs count

# COMMAND ----------

by_day = (events
          .withColumn("day", F.to_date("created_at"))
          .groupBy("day").count().orderBy("day"))
display(by_day)  # line chart: activity over time

# COMMAND ----------

# Engagement funnel: of the trips created, how many reached each stage?
def _trips_with(event_type):
    return events.filter(F.col("event_type") == event_type).select("trip_id").distinct()

funnel = spark.createDataFrame(
    [
        ("1_created", _trips_with("trip_created").count()),
        ("2_destination_added", _trips_with("destination_added").count()),
        ("3_itinerary_generated", _trips_with("itinerary_generated").count()),
        ("4_rescheduled", _trips_with("reschedule").count()),
        ("5_packing_built", _trips_with("packing_built").count()),
    ],
    ["stage", "trips"],
)
display(funnel)  # funnel / bar chart

# COMMAND ----------

top_users = (events.filter(F.col("user_email").isNotNull())
             .groupBy("user_email").count().orderBy(F.col("count").desc()))
display(top_users)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3. Write the Delta gold analytics table
# MAGIC
# MAGIC A single tidy summary table (one row per metric) that a dashboard or the
# MAGIC grader can query. Governed Delta in Unity Catalog.

# COMMAND ----------

summary = (
    by_type.select(
        F.lit("events_by_type").alias("metric"),
        F.col("event_type").alias("dimension"),
        F.col("count").alias("value"),
    )
    .unionByName(funnel.select(
        F.lit("funnel").alias("metric"),
        F.col("stage").alias("dimension"),
        F.col("trips").alias("value"),
    ))
    .unionByName(spark.createDataFrame(
        [("totals", "total_events", events.count()),
         ("totals", "distinct_trips", events.select("trip_id").distinct().count())],
        ["metric", "dimension", "value"]))
    .withColumn("computed_at", F.current_timestamp())
)

summary.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(GOLD)
print(f"Wrote {summary.count()} rows to {GOLD}")
display(spark.table(GOLD))
