-- Migration: alert_rules and alert_events tables
-- Run this in your Supabase SQL editor.

-- ── Alert Rules ───────────────────────────────────────────────────────────────
create table if not exists public.alert_rules (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references auth.users(id) on delete cascade,
  name              text not null,
  metric            text not null,       -- spend | ctr | cpc | roas | impressions
  operator          text not null,       -- gt | lt | gte | lte
  threshold         float not null,
  status            text not null default 'active',  -- active | paused
  campaign_id       text,               -- Meta campaign ID (optional filter)
  trigger_count     int not null default 0,
  last_triggered_at timestamptz,
  created_at        timestamptz not null default now()
);

alter table public.alert_rules enable row level security;

create policy "Users manage their own alert rules"
  on public.alert_rules for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create index if not exists alert_rules_user_id_idx on public.alert_rules (user_id);
create index if not exists alert_rules_status_idx  on public.alert_rules (status);

-- ── Alert Events ──────────────────────────────────────────────────────────────
create table if not exists public.alert_events (
  id              uuid primary key default gen_random_uuid(),
  rule_id         uuid not null references public.alert_rules(id) on delete cascade,
  user_id         uuid not null references auth.users(id) on delete cascade,
  metric          text not null,
  value           float not null,
  threshold       float not null,
  operator        text not null,
  severity        text not null default 'warning',  -- warning | critical | ok
  campaign_name   text not null default '',
  fired_at        timestamptz not null default now(),
  ai_analysis     text
);

alter table public.alert_events enable row level security;

create policy "Users see their own alert events"
  on public.alert_events for all
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);

create index if not exists alert_events_user_id_idx  on public.alert_events (user_id);
create index if not exists alert_events_rule_id_idx  on public.alert_events (rule_id);
create index if not exists alert_events_fired_at_idx on public.alert_events (fired_at desc);
