-- Run this once in Supabase: SQL Editor -> New query -> paste -> Run

create table if not exists cashbook_entries (
  id          bigint generated always as identity primary key,
  entry_date  date        not null default ((now() at time zone 'Asia/Kolkata')::date),
  amount      numeric(14,2),                       -- null until the amount is confirmed
  direction   text        not null default 'out' check (direction in ('in', 'out')),
  mode        text        not null default 'upi' check (mode in ('upi', 'cash')),
  purpose     text,
  category    text,
  party       text,                                -- who was paid / who paid us
  upi_ref     text,                                -- UPI reference, used to catch duplicate screenshots
  notes       text,
  status      text        not null default 'pending' check (status in ('pending', 'done')),
  tg_msg_id   bigint,                              -- bot message tied to this entry (for reply-to)
  created_at  timestamptz not null default now()
);

create unique index if not exists cashbook_entries_upi_ref_uq
  on cashbook_entries (upi_ref) where upi_ref is not null;
create index if not exists cashbook_entries_date_idx on cashbook_entries (entry_date desc);
create index if not exists cashbook_entries_status_idx on cashbook_entries (status);

-- Only the server (service_role key) touches this table; block the public anon key.
alter table cashbook_entries enable row level security;

-- Bills: photos/PDFs live in a private storage bucket; each entry lists its files here.
alter table cashbook_entries add column if not exists bill_paths text[] not null default '{}';
insert into storage.buckets (id, name, public)
values ('cashbook-bills', 'cashbook-bills', false)
on conflict (id) do nothing;
