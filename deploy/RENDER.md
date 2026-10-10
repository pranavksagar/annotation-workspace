# Deploy to Render (free tier)

The whole application runs as **one Docker web service plus one Postgres database**, both defined in `render.yaml`.
The container (`deploy/start.sh`) runs the database migrations, then the preparation worker and the API, which also
serves the web app. Render's free plan has no background workers, which is why the worker lives in the same container.

Total cost: $0 on the Hobby workspace.

## One-time setup

1. **Sign in to Render with your personal GitHub account.** Go to https://render.com → *Get Started* → *GitHub*.
   Use a Hobby (free) workspace. You can skip adding a card, but see "Free-tier limits" below.
2. **Create the services from the Blueprint.** Dashboard → **New → Blueprint**.
3. **Connect GitHub.** Click *Connect GitHub*, then *Install* the Render GitHub App on your personal account. Under
   *Repository access* choose **Only select repositories**, pick **annotation-workspace**, and save.
4. **Pick the repo.** Render finds `render.yaml` and lists two resources: the web service `annotation-workspace`
   and the database `annotation-db`. Name the blueprint (anything) and click **Apply**.
5. **Wait for the first deploy** (about 5–8 minutes for the Docker build). Open the web service → **Logs** and look for:
   - `Running upgrade … -> 0002_legacy_import_map` (the database migrations)
   - `annotation worker started`
   - `Uvicorn running on http://0.0.0.0:10000`

   The service goes **Live** once `/api/annotation/health/live` answers.
6. **Open the app.** The URL is shown at the top of the service page, like `https://annotation-workspace-xxxx.onrender.com`.
   It opens the drawing library.
7. **Try it.** Upload a small PDF or PNG, wait until it says it is ready, open it, draw a **Text region** and check that
   the transcription is filled in by OCR.

You do not need to set any environment variables by hand. The database URL comes from the Blueprint, and the allowed
origin and host come from the service URL that Render provides.

## Updating the app

Every `git push origin main` redeploys automatically (the new container starts, then the old one is stopped).

## Free-tier limits to know about

| Limit | What it means here |
| --- | --- |
| Service sleeps after 15 min without traffic | The first request after a break takes about a minute. Drawings are only prepared (rendered) while the service is awake; an upload wakes it. |
| 512 MB RAM, 0.1 CPU | Rendering is slow for big drawings. `render.yaml` lowers the render DPI to 150, the upload size to 25 MiB and the page size cap so rendering stays inside memory. |
| 5 GB outbound traffic per month | Viewing many large pages uses this up. Without a card on file Render pauses the service until next month instead of charging. |
| **Free Postgres is deleted 30 days after creation** | You get a 14-day grace period to upgrade it. **Back up before day 30** (below) or you lose every drawing and label. There are no automatic backups. |
| 1 GB database | Drawings and page images are stored in the database. About 1 GB fits a few dozen drawings. |
| 750 free hours per month | Enough for one always-awake service. |

## Back up the database

Render → `annotation-db` → **Connect** → copy the **External Database URL**, then on your computer:

```sh
pg_dump "EXTERNAL_DATABASE_URL" --format=custom --file=annotation-backup.dump
```

To restore into a new database: `pg_restore --no-owner --dbname "NEW_DATABASE_URL" annotation-backup.dump`.

## Keeping it long term

- Upgrade `annotation-db` to a paid plan (Basic) to keep the data and get backups.
- Move the web service to the Starter plan so it never sleeps and the worker always runs.
- The app has **no login**. Anyone with the URL can see and edit every drawing, so only share the link with people you
  trust, or put it behind a VPN or access proxy.

## If something goes wrong

- **Deploy stuck on "Deploying"**: open *Logs*. A failed migration or a missing database shows up before the Uvicorn line.
- **"Invalid host header" (400)**: the host check is built from the service URL. If you add a custom domain, set
  `TRUSTED_HOSTS` and `APP_PUBLIC_ORIGIN` on the service to include it (comma separated).
- **A drawing stays FAILED**: the log shows the reason (often the page is too large for 512 MB). Raise
  `MAX_PAGE_PIXELS` / `RENDER_MEMORY_LIMIT_MIB` only if you upgrade the instance size.
- **No OCR suggestions**: check the service has `ENABLE_OCR_SUGGESTIONS=true` and reload the page.
