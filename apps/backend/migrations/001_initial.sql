SET TIME ZONE 'UTC';

CREATE TABLE IF NOT EXISTS events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind text NOT NULL,
    source text NOT NULL,
    idempotency_key text NOT NULL,
    event_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    raw jsonb NOT NULL,
    normalized jsonb NOT NULL,
    late boolean NOT NULL DEFAULT false,
    UNIQUE (source, idempotency_key),
    CHECK (jsonb_typeof(raw) = 'object'),
    CHECK (jsonb_typeof(normalized) = 'object')
);

CREATE INDEX IF NOT EXISTS events_kind_event_at_idx
    ON events (kind, event_at DESC);

CREATE TABLE IF NOT EXISTS candles (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument text NOT NULL,
    interval text NOT NULL CHECK (interval IN ('1m', '5m')),
    bucket_start timestamptz NOT NULL,
    open numeric NOT NULL,
    high numeric NOT NULL,
    low numeric NOT NULL,
    close numeric NOT NULL,
    volume numeric CHECK (volume IS NULL OR volume >= 0),
    source text NOT NULL,
    finalized boolean NOT NULL DEFAULT true,
    observation_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    UNIQUE (instrument, interval, bucket_start, source),
    CHECK (high >= open AND high >= close AND low <= open AND low <= close),
    CHECK (jsonb_typeof(observation_ids) = 'array')
);

CREATE INDEX IF NOT EXISTS candles_instrument_interval_bucket_idx
    ON candles (instrument, interval, bucket_start DESC);

CREATE TABLE IF NOT EXISTS health_transitions (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subsystem text NOT NULL,
    state text NOT NULL CHECK (state IN ('HEALTHY', 'DEGRADED', 'STALE', 'BLOCKED')),
    reason text,
    observed_at timestamptz NOT NULL,
    idempotency_key text NOT NULL,
    UNIQUE (subsystem, idempotency_key)
);

CREATE INDEX IF NOT EXISTS health_transitions_subsystem_observed_idx
    ON health_transitions (subsystem, observed_at DESC);
