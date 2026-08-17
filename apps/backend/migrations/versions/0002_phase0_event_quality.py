"""Add explicit event outcomes and candle evidence quality.

Revision ID: 0002_phase0_event_quality
Revises: 0001_current_foundation_baseline
"""

from alembic import op

revision = "0002_phase0_event_quality"
down_revision = "0001_current_foundation_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE events ADD COLUMN IF NOT EXISTS event_identity text;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS processed_at timestamptz;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS outcome text;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS feed_generation text;
    ALTER TABLE events ADD COLUMN IF NOT EXISTS rejection_detail text;
    UPDATE events SET event_identity = idempotency_key WHERE event_identity IS NULL;
    UPDATE events SET processed_at = received_at WHERE processed_at IS NULL;
    UPDATE events SET outcome = CASE WHEN late THEN 'OTHER' ELSE 'ACCEPTED' END
        WHERE outcome IS NULL;
    ALTER TABLE events ALTER COLUMN event_identity SET NOT NULL;
    ALTER TABLE events ALTER COLUMN processed_at SET NOT NULL;
    ALTER TABLE events ALTER COLUMN outcome SET NOT NULL;
    ALTER TABLE events ADD CONSTRAINT events_outcome_check CHECK (outcome IN (
        'ACCEPTED', 'DUPLICATE', 'STALE', 'FUTURE_TIMESTAMP',
        'INACTIVE_FEED_GENERATION', 'OUT_OF_ORDER', 'MARKET_CLOSED', 'MALFORMED',
        'MISSING_REQUIRED_DATA', 'OTHER'));
    CREATE INDEX IF NOT EXISTS events_outcome_event_at_idx ON events (outcome, event_at DESC);
    CREATE INDEX IF NOT EXISTS events_generation_event_at_idx
        ON events (feed_generation, event_at DESC) WHERE feed_generation IS NOT NULL;
    CREATE UNIQUE INDEX IF NOT EXISTS events_accepted_identity_idx
        ON events (source, event_identity) WHERE outcome = 'ACCEPTED';

    ALTER TABLE candles DROP CONSTRAINT IF EXISTS candles_interval_check;
    ALTER TABLE candles ADD CONSTRAINT candles_interval_check
        CHECK (interval IN ('1m', '5m', '15m'));
    ALTER TABLE candles ADD COLUMN IF NOT EXISTS completeness text NOT NULL DEFAULT 'COMPLETE';
    ALTER TABLE candles ADD COLUMN IF NOT EXISTS expected_components integer NOT NULL DEFAULT 1;
    ALTER TABLE candles ADD COLUMN IF NOT EXISTS actual_components integer NOT NULL DEFAULT 1;
    ALTER TABLE candles ADD COLUMN IF NOT EXISTS source_event_at_min timestamptz;
    ALTER TABLE candles ADD COLUMN IF NOT EXISTS source_event_at_max timestamptz;
    ALTER TABLE candles ADD CONSTRAINT candles_completeness_check
        CHECK (completeness IN ('COMPLETE', 'INCOMPLETE'));
    ALTER TABLE candles ADD CONSTRAINT candles_components_check
        CHECK (expected_components > 0 AND actual_components > 0
               AND actual_components <= expected_components
               AND (completeness <> 'COMPLETE'
                    OR (actual_components = expected_components AND finalized)));
    """)


def downgrade() -> None:
    raise RuntimeError("forward-only migration; restore the pre-migration backup to recover")
