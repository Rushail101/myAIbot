create extension if not exists pgcrypto;
create table if not exists public.transactions (
 id uuid primary key default gen_random_uuid(), amount numeric(14,2) not null check(amount > 0),
 payee text, purpose text, payment_method text not null check(payment_method in ('UPI','Cash','Bank Transfer','Card','Other')),
 payment_status text not null default 'unknown', upi_reference text unique, transaction_date date not null default current_date,
 source text not null default 'manual', raw_text text, review_status text not null default 'pending' check(review_status in ('pending','confirmed','needs_review','voided')),
 created_by_telegram_id bigint, category text, notes text, created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create index if not exists transactions_date_idx on public.transactions(transaction_date desc);
create index if not exists transactions_payee_idx on public.transactions(payee);
create index if not exists transactions_status_idx on public.transactions(review_status);
create table if not exists public.vendor_rules (
 id uuid primary key default gen_random_uuid(), match_type text not null check(match_type in ('payee_exact','payee_contains','upi_id_contains','text_contains')),
 match_value text not null, default_purpose text, default_category text, default_payment_method text, enabled boolean not null default true,
 created_at timestamptz not null default now(), unique(match_type, match_value)
);
create table if not exists public.transaction_audit (
 id uuid primary key default gen_random_uuid(), transaction_id uuid, action text not null, actor_telegram_id bigint,
 details jsonb not null default '{}'::jsonb, created_at timestamptz not null default now()
);
alter table public.transactions enable row level security;
alter table public.vendor_rules enable row level security;
alter table public.transaction_audit enable row level security;
-- Bot uses service-role key on the server only. Configure Auth and RLS before exposing dashboard reads.
