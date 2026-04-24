-- Migration 023 - geoblock rules table. Admin-editable list of countries
-- and subdivisions where Delfi is not available.
--
-- Background
-- ----------
-- Prediction markets are regulated in several jurisdictions (US / CFTC,
-- UK / Gambling Commission, France / ANJ, Belgium, Singapore, Thailand,
-- Poland, Taiwan, Ontario-Canada). We need a single source of truth
-- the proxy can check before letting anyone touch the product, and the
-- admin can edit without a deploy.
--
-- Schema
-- ------
-- One row per (country_code, subdivision_code) pair. Subdivision is
-- nullable; NULL means "block the whole country". Unique index uses
-- COALESCE so NULL collides with NULL for a given country code.
--
-- ISO 3166-1 alpha-2 for country_code (two uppercase letters).
-- ISO 3166-2 without the country prefix for subdivision_code (e.g. the
-- Ontario code 'CA-ON' is stored here as country='CA', subdivision='ON').
--
-- RLS
-- ---
-- Public SELECT so the edge proxy can read the list without a service
-- role token. Inserts and deletes are admin-only.
--
-- Seed
-- ----
-- A default list matching Polymarket's current known restrictions.
-- Note: the US is NOT blocked. Polymarket acquired QCX (a CFTC-registered
-- Designated Contract Market) in late 2024 and relaunched as Polymarket
-- US; US residents have legal access via the CFTC-regulated venue. Admin
-- can add US (or any state subdivision) back from the dashboard if that
-- posture changes.

BEGIN;

CREATE TABLE IF NOT EXISTS geoblock_rules (
    id               SERIAL       PRIMARY KEY,
    country_code     CHAR(2)      NOT NULL CHECK (country_code = UPPER(country_code)),
    subdivision_code VARCHAR(10)  CHECK (subdivision_code IS NULL OR subdivision_code = UPPER(subdivision_code)),
    reason           TEXT,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by       UUID         REFERENCES auth.users(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS geoblock_rules_unique
    ON geoblock_rules (country_code, COALESCE(subdivision_code, ''));

CREATE INDEX IF NOT EXISTS geoblock_rules_country_idx
    ON geoblock_rules (country_code);

ALTER TABLE geoblock_rules ENABLE ROW LEVEL SECURITY;

-- Readable by anyone authenticated or not; the edge proxy needs it
-- before any session exists.
DROP POLICY IF EXISTS geoblock_rules_select_public ON geoblock_rules;
CREATE POLICY geoblock_rules_select_public
    ON geoblock_rules FOR SELECT
    USING (true);

-- Admin-only writes. The admin API route double-checks is_admin
-- server-side so this policy is defense-in-depth.
DROP POLICY IF EXISTS geoblock_rules_admin_write ON geoblock_rules;
CREATE POLICY geoblock_rules_admin_write
    ON geoblock_rules FOR ALL
    USING (
        EXISTS (
            SELECT 1 FROM user_config uc
             WHERE uc.user_id = auth.uid()
               AND uc.is_admin = true
        )
    )
    WITH CHECK (
        EXISTS (
            SELECT 1 FROM user_config uc
             WHERE uc.user_id = auth.uid()
               AND uc.is_admin = true
        )
    );

GRANT SELECT ON geoblock_rules TO anon, authenticated;
GRANT USAGE, SELECT ON SEQUENCE geoblock_rules_id_seq TO authenticated;

-- Seed the defaults. ON CONFLICT DO NOTHING so re-runs are no-ops and
-- admins can safely delete rows without them reappearing.
INSERT INTO geoblock_rules (country_code, subdivision_code, reason)
VALUES
    ('GB', NULL, 'UK residents: Gambling Commission restrictions on prediction markets'),
    ('FR', NULL, 'France: ANJ restrictions on unlicensed betting'),
    ('BE', NULL, 'Belgium: Gaming Commission restrictions'),
    ('SG', NULL, 'Singapore: Gambling Control Act'),
    ('TH', NULL, 'Thailand: Gambling Act prohibitions'),
    ('PL', NULL, 'Poland: unlicensed gambling restrictions'),
    ('TW', NULL, 'Taiwan: Social Order Maintenance Act'),
    ('CA', 'ON', 'Ontario: iGaming Ontario (AGCO) exclusivity framework')
ON CONFLICT DO NOTHING;

NOTIFY pgrst, 'reload schema';

COMMIT;
