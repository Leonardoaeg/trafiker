-- Migration: meta_accounts table
-- Stores per-user Meta Ads OAuth connections.
-- Run this in your Supabase SQL editor.

create table if not exists public.meta_accounts (
  id                  uuid primary key default gen_random_uuid(),
  user_id             uuid not null references auth.users(id) on delete cascade,
  meta_user_id        text not null default '',  -- Meta's internal user ID (for data deletion)
  meta_ad_account_id  text not null,             -- e.g. "act_123456789"
  name                text not null,
  currency            text not null default '',
  timezone            text not null default '',
  status              text not null default 'active', -- active | expired | revoked
  access_token        text not null,             -- long-lived token (60 days)
  token_expires_at    timestamptz,
  last_synced_at      timestamptz,
  connected_at        timestamptz not null default now(),

  unique (user_id, meta_ad_account_id)
);

-- Index for data deletion lookup by Meta user ID
create index if not exists meta_accounts_meta_user_id_idx on public.meta_accounts (meta_user_id);

-- Only the owning user can see/modify their own rows
alter table public.meta_accounts enable row level security;

create policy "Users can manage their own meta accounts"
  on public.meta_accounts
  for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

-- Index for fast lookups by user
create index if not exists meta_accounts_user_id_idx on public.meta_accounts (user_id);
