-- Adds bill storage to an existing cashbook database.
-- Run once in Supabase: SQL Editor -> New query -> paste -> Run. Safe to run twice.

-- Each entry keeps the list of its bill files (several pages are allowed).
alter table cashbook_entries add column if not exists bill_paths text[] not null default '{}';

-- Private bucket for the files. Only the server (service_role key) can read or write it;
-- the dashboard gets short-lived signed links through the API.
insert into storage.buckets (id, name, public)
values ('cashbook-bills', 'cashbook-bills', false)
on conflict (id) do nothing;
