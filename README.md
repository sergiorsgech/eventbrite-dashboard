# Eventbrite Dashboard — hosted, auto-updating

This folder is a ready-to-push GitHub repo. It rebuilds `index.html` (the
Eventbrite dashboard) twice a day using GitHub Actions, and Azure Static Web
Apps serves it at a public URL you can share with your team.

How it fits together:

1. **GitHub Actions** (`.github/workflows/update-dashboard.yml`) runs on a
   schedule, calls the Eventbrite API, regenerates `index.html`, and commits
   it back to the repo.
2. **Azure Static Web Apps** watches this repo and redeploys automatically
   every time `index.html` changes — so a few minutes after each scheduled
   run, the live link shows fresh data.

You only need to do the setup once. After that, it runs itself.

## Step 1 — Push this to a new GitHub repo

1. Go to https://github.com/new and create a new repository (public or
   private both work — Azure Static Web Apps free tier supports either).
   Name it something like `eventbrite-dashboard`. Don't initialize it with a
   README (you already have one here).
2. On your Mac, open Terminal in this folder and run:
   ```
   cd /Users/sergiosoto/Desktop/Claude/eventbrite-dashboard
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/YOUR-USERNAME/eventbrite-dashboard.git
   git push -u origin main
   ```
   (Replace the URL with the one GitHub shows you after creating the repo.)

## Step 2 — Add your Eventbrite token as a GitHub secret

Your token should never be committed into the repo itself — GitHub Actions
reads it from an encrypted secret instead.

1. In your new repo on GitHub: **Settings → Secrets and variables → Actions
   → New repository secret**.
2. Name: `EVENTBRITE_TOKEN`
3. Value: your Eventbrite Private Token (the same one you've been using
   locally).
4. Save.

## Step 3 — Create the Azure Static Web App

1. In the Azure Portal, search for **Static Web Apps** and click **Create**.
2. Fill in:
   - **Resource group**: use an existing one or create a new one.
   - **Name**: e.g. `eventbrite-dashboard`.
   - **Plan type**: **Free**.
   - **Deployment source**: **GitHub** — sign in and authorize if prompted,
     then pick your `eventbrite-dashboard` repo and the `main` branch.
   - **Build presets**: **Custom**.
   - **App location**: `/`
   - **Api location**: *(leave blank)*
   - **Output location**: *(leave blank)*
3. Click **Review + create**, then **Create**.

Azure will automatically add a *second* workflow file to your repo (something
like `.github/workflows/azure-static-web-apps-<random-name>.yml`) and a
matching deployment secret. You don't need to touch either — that workflow's
only job is "redeploy whenever the repo changes," and it'll fire right after
the dashboard-update workflow commits new data.

## Step 4 — Get your link

Once the Static Web App resource finishes creating (a minute or two), the
Azure Portal shows a **URL** field on the resource's Overview page, something
like `https://calm-stone-0123abcd.azurestaticapps.net`. That's the link to
share with your coworker — it'll always show the latest run.

## Step 5 — Trigger the first run

Don't want to wait for the schedule? In your GitHub repo, go to the
**Actions** tab → **Update Eventbrite dashboard** → **Run workflow** → **Run
workflow**. Watch it go green (takes a minute or two depending on how many
events need fresh ticket stats), then check the Azure link — it updates
automatically a moment later.

## Schedule

The dashboard rebuilds at 8:00 AM and 8:00 PM Pacific time (15:00 and 03:00
UTC). GitHub Actions cron schedules are fixed in UTC and don't shift for
daylight saving, so the Pacific-time equivalent will drift by an hour for a
few weeks around the March/November clock changes. To change the times,
edit the two `cron:` lines in `.github/workflows/update-dashboard.yml`.

## Notes

- The workflow also commits `.eventbrite_ticket_cache.json` back to the repo
  so completed-event ticket stats aren't re-fetched every run — this keeps
  each scheduled run fast.
- If a run ever fails (e.g. Eventbrite is down, or the token expires), check
  the **Actions** tab for the error — the site just keeps showing the last
  successful build until the next run succeeds.
- To rotate your Eventbrite token later, just update the `EVENTBRITE_TOKEN`
  secret in GitHub — no code changes needed.
