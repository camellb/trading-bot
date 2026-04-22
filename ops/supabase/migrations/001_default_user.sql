-- Phase D migration 001 — seed the synthetic "default" user.
--
-- Every per-user table will reference auth.users(id). The existing local
-- database predates multi-tenancy, so all of its rows must be assigned to
-- a single owner during the Phase E cutover. That owner is this synthetic
-- user with a fixed UUID so subsequent migrations can reference it by
-- literal instead of needing a lookup.
--
-- Supabase Auth manages auth.users via the auth service. A direct INSERT
-- bypasses the normal signup flow — acceptable here because this row is
-- not meant to be signed into; it exists only to anchor legacy rows.
--
-- Idempotent: ON CONFLICT DO NOTHING so re-running the migration is safe.

INSERT INTO auth.users (
    id,
    instance_id,
    aud,
    role,
    email,
    encrypted_password,
    email_confirmed_at,
    raw_app_meta_data,
    raw_user_meta_data,
    is_super_admin,
    created_at,
    updated_at,
    confirmation_token,
    recovery_token,
    email_change_token_new,
    email_change
)
VALUES (
    '00000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-000000000000',
    'authenticated',
    'authenticated',
    'default@delfi.local',
    '',
    NOW(),
    '{"provider":"system","providers":["system"]}',
    '{"synthetic":true,"purpose":"legacy_data_owner"}',
    FALSE,
    NOW(),
    NOW(),
    '',
    '',
    '',
    ''
)
ON CONFLICT (id) DO NOTHING;
