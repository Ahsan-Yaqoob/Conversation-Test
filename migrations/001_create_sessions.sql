-- Run this once in the Supabase SQL Editor:
-- https://supabase.com/dashboard/project/bonniwgetitjjcggswel/sql/new

create table public.sessions (
  session_id text not null,
  status text null,
  services jsonb null,
  msg_count integer null,
  scraped_at text null,
  conversation jsonb null,
  result_json jsonb null,
  reference_data jsonb null,
  analysis_status text null,
  analysis_summary text null,
  analysis_issues jsonb null,
  extractor_rating integer null,
  rating_reason text null,
  analyzed_at text null,
  db_updated_at text null,
  created_at timestamp with time zone null default now(),
  session_created_at text null,
  dismissed_issues jsonb null default '[]'::jsonb,
  constraint sessions_pkey primary key (session_id)
) TABLESPACE pg_default;

create index IF not exists idx_sessions_scraped_at on public.sessions using btree (scraped_at desc) TABLESPACE pg_default;
create index IF not exists idx_sessions_analysis_status on public.sessions using btree (analysis_status) TABLESPACE pg_default;
create index IF not exists idx_sessions_extractor_rating on public.sessions using btree (extractor_rating) TABLESPACE pg_default;
create index IF not exists idx_sessions_session_created_at on public.sessions using btree (session_created_at desc) TABLESPACE pg_default;
