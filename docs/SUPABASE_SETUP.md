# Durable storage setup (Supabase)

The dashboard remembers every lot it has ever seen so that, week to week, it can
show you what's **new** and whose **price changed**. To survive Streamlit Cloud
redeploys, that data lives in a free Supabase Postgres database.

One-time setup, ~10 minutes:

## 1. Create a Supabase project
1. Go to https://supabase.com and sign up (free).
2. Click **New project**. Give it any name, set a database password, pick a region near you.
3. Wait ~1 minute for it to provision.

## 2. Create the table
1. In the project, open **SQL Editor** (left sidebar) → **New query**.
2. Paste and **Run**:

   ```sql
   create table if not exists listings (
       lot_id     text primary key,
       data       jsonb not null,
       updated_at timestamptz not null default now()
   );
   ```

That's the whole schema — one row per lot, the full listing stored as JSON.

## 3. Get your credentials
1. Open **Project Settings** (gear icon) → **API**.
2. Copy the **Project URL** (e.g. `https://abcd1234.supabase.co`).
3. Copy the **`service_role`** key (under *Project API keys*). This key is used
   server-side only (never sent to the browser), and it bypasses row-level
   security, so no extra policies are needed.

## 4. Add them to the app

**On Streamlit Cloud:** open your app → **Manage app** → **Settings** → **Secrets**,
and paste:

```toml
SUPABASE_URL = "https://abcd1234.supabase.co"
SUPABASE_KEY = "your-service_role-key"
```

Save. The app reboots automatically.

**For local development:** copy `.streamlit/secrets.toml.example` to
`.streamlit/secrets.toml` and fill in the same two values. (This file is
gitignored.)

## 5. Confirm it's live
In the app sidebar you'll see:

> 💾 Armazenamento: Supabase (durável)

If it instead says *arquivo local*, the credentials aren't being picked up —
re-check the two secret names are exactly `SUPABASE_URL` and `SUPABASE_KEY`.

Now your searches persist across redeploys. You can also browse the raw data
anytime in Supabase → **Table editor** → `listings`.
