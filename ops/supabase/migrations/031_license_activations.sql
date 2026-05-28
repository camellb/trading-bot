-- 031_license_activations.sql
-- ----------------------------------------------------------------------
-- Per-license device-binding slot.
--
-- One Delfi license activates on exactly ONE device at a time. This
-- table is the single source of truth for "which device currently
-- owns the slot for license X".
--
-- See:
--   - apps/web/app/api/license/claim-device/route.ts
--   - apps/web/app/api/license/release-device/route.ts
--   - apps/web/app/api/license/check/route.ts (extended)
--   - Delfibot/bot/engine/device_id.py
--   - Obsidian/Delfi/50_Feedback/license_one_device_at_a_time.md
--
-- Idempotent: re-running is safe. The table either exists (no-op)
-- or it doesn't (create).
-- ----------------------------------------------------------------------

BEGIN;

CREATE TABLE IF NOT EXISTS public.license_activations (
    -- PRIMARY KEY = the licence id. There can be AT MOST one active
    -- device per licence; the slot is the row. Releasing the slot
    -- (Settings -> License -> Log out) deletes the row. Claiming the
    -- slot from a different device either fails (409) or, with
    -- force=true, overwrites the existing row in place.
    license_id   UUID PRIMARY KEY
                 REFERENCES public.licenses(id) ON DELETE CASCADE,

    -- Opaque, server-side. The desktop sends a SHA-256 hash of the
    -- platform machine identifier (IOPlatformUUID on macOS,
    -- MachineGuid on Windows, /etc/machine-id elsewhere). The raw
    -- identifier never leaves the user's machine.
    device_id    TEXT NOT NULL,

    -- Short human-readable label so the conflict-prompt UI can say
    -- "currently active on MacBook Pro" instead of just a hash.
    -- Best-effort: macOS scutil ComputerName, Windows registry
    -- ComputerName, hostname elsewhere. Truncated to 80 chars on
    -- the server so a long name can't blow up the row size.
    device_label TEXT,

    -- When the slot was first claimed by this device.
    activated_at TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Heartbeat: refreshed on every periodic /api/license/check
    -- call from THIS device. Useful for "last seen X days ago" in
    -- the conflict prompt + for auditing.
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lookup-by-device for diagnostics ("which licence does this
-- fingerprint own?"). Not on the hot path; the claim-device flow
-- always queries by license_id.
CREATE INDEX IF NOT EXISTS license_activations_device_idx
    ON public.license_activations(device_id);

-- Stale-row index for an eventual cleanup job that releases slots
-- with no heartbeat for 90+ days. Not yet wired but cheap to keep.
CREATE INDEX IF NOT EXISTS license_activations_last_seen_idx
    ON public.license_activations(last_seen_at DESC);

COMMIT;

-- Sanity check: the new table exists and references licenses.
SELECT
    c.relname    AS table_name,
    a.attname    AS column_name,
    t.typname    AS type
FROM pg_attribute a
JOIN pg_class c     ON a.attrelid = c.oid
JOIN pg_type  t     ON a.atttypid = t.oid
WHERE c.relname = 'license_activations'
  AND a.attnum > 0
ORDER BY a.attnum;
