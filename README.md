# Job Hacker

A self-hosted job-hunt workbench. Paste a link, get the job saved, move it through your pipeline, and tailor your resume and cover letter for it.

- **Add from a link.** Reads LinkedIn (public view), MyCareersFuture, and any page with standard job data (Greenhouse, Lever, Workday, JobStreet, most company career sites). Pages without it are read by Claude.
- **Paste or type when a site won't share.** If a page needs a login, the app switches to "Paste text": paste the whole post and Claude fills in the fields. "Manual" lets you type everything yourself.
- **Pipeline:** Saved → Applied → Interviewing → Offer → Closed, shown as a flow strip you can tap to filter.
- **Fit check:** a 1–10 score against your resume and what you're looking for, with strengths, gaps and keywords.
- **Tailored resume and cover letter** per job: copy the text, edit it, or download it as a PDF or Word file.
- **Phone-first:** large type, installable to your home screen, appears in the Android share menu.
- **Claude or your local model:** every AI action has a Claude / Local switch. Local uses Ollama on the same machine (for example `qwen3:14b`), so it costs nothing per use.
- **Overnight resumes:** pick jobs for tonight's list (in Settings, or "Add to tonight's list" on a job) and at a time you choose (01:00 by default) their resumes, fit checks or cover letters are made with the local model or Claude. Only picked jobs are done.
- **Bulk add:** paste many links at once, fill in missing details for imported jobs, or import a CSV in the old tracker's format.
- **Profiles** for more than one person, **backup/restore**, **CSV import**, and an **API** for n8n.

One container, one SQLite file. No separate database or nginx needed.

## Run it

```sh
cp .env.example .env      # set APP_PASSWORD, SECRET_KEY, ANTHROPIC_API_KEY, API_TOKEN
docker compose up -d      # or: podman compose up -d
```

Open http://localhost:8080. Data lives in the `jobhacker_data` volume (`/data/jobhacker.db` inside the container).

To build locally instead of pulling the published image: `docker compose up -d --build`.

## Deploying on the Mac mini

1. Install Podman Desktop (or Docker Desktop) and start the machine.
2. Get the files:
   ```sh
   git clone https://github.com/thisisyazid/Job-hacker-2.git && cd Job-hacker-2
   cp .env.example .env && nano .env
   ```
3. GitHub Actions publishes `ghcr.io/thisisyazid/job-hacker-2:latest` for both Intel and Apple Silicon. Either make the package public (GitHub → your profile → Packages → job-hacker-2 → Package settings → Change visibility) or log in once:
   ```sh
   podman login ghcr.io -u thisisyazid   # password: a GitHub token with read:packages
   ```
4. Start it: `podman compose up -d`
5. Expose it the same way as the quiz app: add a Cloudflare Tunnel public hostname pointing at `http://localhost:8080`. Keep `APP_PASSWORD` set whenever it's reachable from outside.

Updating later: `podman compose pull && podman compose up -d`.

## Using the local model (Ollama)

The app talks to Ollama at `OLLAMA_URL` (default `http://host.docker.internal:11434`, which reaches the Mac from inside the container). Check the container can see it:

```sh
docker exec jobhacker python -c "import urllib.request;print(urllib.request.urlopen('http://host.docker.internal:11434/api/tags').read()[:200])"
```

If that prints your models, the Local option appears in the app. If not, Settings shows the reason. A 14B model on an M4 takes roughly 1–4 minutes per resume, so the overnight run is the comfortable way to use it.

## Bringing over the old tracker's data

On the machine running the old job-tracker:

```sh
curl -O https://raw.githubusercontent.com/thisisyazid/Job-hacker-2/main/scripts/export_old_tracker.py
podman exec -i $(podman ps --format '{{.Names}}' | grep backend) python - < export_old_tracker.py > old-tracker.json
```

Then in the new app: **Settings → Import backup → old-tracker.json**. Profiles, base resumes, jobs, notes and generated documents come across. Old categories become tags; "Not started" becomes Saved, "In progress" Applied, "Abandon" Closed. Importing twice is safe: jobs with the same link are skipped.

## Adding jobs from your phone

- **Android:** open the site in Chrome → menu → *Add to Home screen*. Job Hacker then shows up when you share a link from LinkedIn or Chrome.
- **iPhone:** add to Home Screen from Safari's share menu. For one-tap saving, create a Shortcut that receives URLs from the share sheet and opens `https://<your-host>/?add=<Shortcut Input>`.

## Automations (n8n)

```
POST /api/jobs
X-API-Key: <API_TOKEN>
X-User: Yazid            # optional, profile name or id; defaults to the first profile
Content-Type: application/json

{"title": "...", "url": "...", "company": "...", "location": "...", "description": "..."}
```

Returns 201 for a new job. If the link is already saved, empty fields on the saved job are filled in and you get 200 with `"duplicate": true`. Interactive API docs are at `/api/docs` once logged in.

## Log files

Job Hacker writes one log file per day to `logs/` in the data folder (`/data/logs` in Docker), named `jobhacker-YYYY-MM-DD.log`, and keeps the last 30 days (`LOG_KEEP_DAYS`). Each line says what happened: CSV and backup imports with what every row became (added, already saved as which job, taken down, no title), links read and how, AI calls with timings, overnight runs, deletions and errors with tracebacks. API keys, resumes and job descriptions are never written.

Download them from **Settings → Log files**, or on the Mac mini:

```
docker compose cp jobhacker:/data/logs ./logs
```

## Development

```sh
pip install -r requirements.txt pytest
DATA_DIR=./data uvicorn app.main:app --reload --port 8080
pytest -q
```

The frontend is plain HTML/CSS/JS in `static/`, no build step. Every push to `main` runs the tests, then publishes the image.

### Layout

```
app/main.py         API routes, import/export, serves the frontend
app/scrape.py       link → job details (site APIs, JSON-LD, Claude fallback)
app/ai.py           extraction, fit score, resume, cover letter
app/db.py           SQLite models
static/             the app (index.html, app.css, app.js, PWA files)
scripts/            one-off tools (old tracker export)
```
