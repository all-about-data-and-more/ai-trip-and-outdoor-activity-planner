-- Core Lakebase schema for the AI Trip & Outdoor Activity Planner.
--
-- REFERENCE ONLY. You do NOT need to run this by hand: lakebase.py's
-- ensure_*_table() helpers (called by ensure_all_tables() on app startup /
-- first request) create the exact same tables idempotently. This file mirrors
-- them 1:1 for inspection, grading, and manual setup, matching the weather
-- app's sql/ convention.

-- 1. users -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    email         TEXT PRIMARY KEY,
    name          TEXT,
    home_location TEXT,
    preferences   JSONB NOT NULL DEFAULT '{}'::jsonb,   -- interests, pace, dietary, etc.
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. trips -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trips (
    id         TEXT PRIMARY KEY,
    user_email TEXT NOT NULL,
    title      TEXT NOT NULL,
    start_date DATE,
    end_date   DATE,
    notes      TEXT,                                     -- free-text user notes (embedded)
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_trips_user_email ON trips (user_email);

-- 3. destinations ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS destinations (
    id           TEXT PRIMARY KEY,
    trip_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    lat          DOUBLE PRECISION,
    lon          DOUBLE PRECISION,
    description  TEXT,                                   -- Wikimedia summary (embedded)
    wiki_url     TEXT,
    arrival_date DATE,
    depart_date  DATE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_destinations_trip_id ON destinations (trip_id);

-- 4. activities ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS activities (
    id             TEXT PRIMARY KEY,
    destination_id TEXT NOT NULL,
    name           TEXT NOT NULL,
    category       TEXT,                                 -- e.g. 'museum', 'hike', 'park'
    is_outdoor     BOOLEAN NOT NULL DEFAULT FALSE,       -- drives weather-based rescheduling
    description    TEXT,                                 -- Wikimedia/attraction text (embedded)
    requirements   TEXT,                                 -- gear/conditions needed (embedded)
    tags           JSONB NOT NULL DEFAULT '[]'::jsonb,
    source         TEXT NOT NULL DEFAULT 'user',         -- 'wikimedia' | 'user'
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_activities_destination_id ON activities (destination_id);
CREATE INDEX IF NOT EXISTS idx_activities_is_outdoor ON activities (is_outdoor);

-- 5. itinerary_items -------------------------------------------------------
CREATE TABLE IF NOT EXISTS itinerary_items (
    id             TEXT PRIMARY KEY,
    trip_id        TEXT NOT NULL,
    destination_id TEXT,
    activity_id    TEXT,
    title          TEXT NOT NULL,
    day_date       DATE,
    start_time     TEXT,
    end_time       TEXT,
    is_outdoor     BOOLEAN NOT NULL DEFAULT FALSE,
    status         TEXT NOT NULL DEFAULT 'planned',      -- 'planned' | 'rescheduled' | 'done'
    notes          TEXT,                                 -- includes reschedule reasoning
    sort_order     INT NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_itinerary_items_trip_id ON itinerary_items (trip_id);
CREATE INDEX IF NOT EXISTS idx_itinerary_items_day_date ON itinerary_items (day_date);

-- 6. weather_snapshots -----------------------------------------------------
CREATE TABLE IF NOT EXISTS weather_snapshots (
    id                TEXT PRIMARY KEY,
    destination_id    TEXT NOT NULL,
    snapshot_date     DATE NOT NULL,
    temp_high         DOUBLE PRECISION,
    temp_low          DOUBLE PRECISION,
    precip_chance_pct DOUBLE PRECISION,
    precip_amount     DOUBLE PRECISION,
    wind_max          DOUBLE PRECISION,
    aqi               DOUBLE PRECISION,                  -- US AQI (Open-Meteo air quality)
    pm2_5             DOUBLE PRECISION,
    uv_index_max      DOUBLE PRECISION,
    pollen            JSONB,                             -- grass/birch/etc. where available
    conditions        TEXT,
    payload           JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- One snapshot per destination per day; re-fetching upserts.
CREATE UNIQUE INDEX IF NOT EXISTS uq_weather_snapshots_dest_date
    ON weather_snapshots (destination_id, snapshot_date);

-- 7. packing_items ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS packing_items (
    id         TEXT PRIMARY KEY,
    trip_id    TEXT NOT NULL,
    category   TEXT,                                     -- 'clothing' | 'gear' | 'documents' ...
    item       TEXT NOT NULL,
    reason     TEXT,                                     -- why the agent added it
    quantity   INT NOT NULL DEFAULT 1,
    packed     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_packing_items_trip_id ON packing_items (trip_id);
