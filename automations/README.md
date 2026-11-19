# LinkedIn jobs from Apify, straight into Job Hacker (n8n)

`n8n-apify-linkedin.json` is an n8n workflow that runs an Apify LinkedIn jobs scraper every morning and sends the results to Job Hacker. There's no CSV to save or upload.

Job Hacker treats these jobs exactly like a CSV import:
- Jobs you already have, archived ones included, are skipped.
- Reposts get their posted date updated.
- Look-alikes of saved jobs are flagged under **Possible duplicates**.
- Easy Apply is read from the scraper's apply type.
- Jobs that arrive without a description are read from their postings in the background, without AI.

Every job and what became of it is written to that day's log file.

## 1. Job Hacker: set an API token

In Job Hacker's `.env`, set `API_TOKEN` to a long random string:

```sh
openssl rand -hex 24
```

Then restart Job Hacker:

```sh
docker compose up -d
```

n8n sends this token in the `X-API-Key` header.

## 2. n8n: add two credentials

In n8n, go to **Credentials → Add credential → Header Auth** and create two:

| Credential name | Header name | Value |
|---|---|---|
| `Apify API` | `Authorization` | `Bearer <your Apify API token>` (Apify → Settings → API & Integrations) |
| `Job Hacker API` | `X-API-Key` | the `API_TOKEN` from step 1 |

## 3. n8n: import the workflow

**Workflows → Import from File**, and pick `n8n-apify-linkedin.json`. Open each HTTP node and select the matching credential. n8n flags any node that still needs one.

## 4. Fill in the Settings node

- **apifyActor**: the scraper's ID, written `username~actor-name`. You'll find it on the actor's page on Apify, for example `curious_coder~linkedin-jobs-scraper` or `bebity~linkedin-jobs-scraper`. Use the actor you already run.
- **actorInput**: the scraper's input as JSON. On Apify, open the actor, set up your search in the **Input** tab, switch to the **JSON** view, copy everything and paste it here. Your LinkedIn search URLs, the number of jobs and so on all go in this input. Adding `f_TPR=r86400` to a LinkedIn search URL keeps it to jobs posted in the last 24 hours.
- **jobHackerUrl**: where n8n can reach Job Hacker.
  - n8n in Docker on the same Mac: `http://host.docker.internal:8080`.
  - n8n in the same `docker compose` project as Job Hacker: `http://jobhacker:8080`.
  - n8n Cloud: your public Job Hacker address, such as the Cloudflare Tunnel hostname.
- **profile**: the Job Hacker profile to add jobs to, by name. Leave it empty to use the first profile.

## 5. Test, then switch it on

Click **Run now (test)**, then **Execute workflow**. The run takes as long as the scraper does, usually a few minutes; the workflow checks Apify once a minute until it's done.

The last node, **Send to Job Hacker**, shows the result:

```json
{"rows": 87, "added": 23, "duplicates": 64, "reposted": 5, "possible_duplicates": 2, "easy_apply_marked": 0, ...}
```

When that looks right, turn the workflow on (the toggle at the top). It then runs every morning at 7. To change the time, edit **Every morning at 7**.

## Already scheduling the scraper on Apify?

You can keep Apify's own schedule and just collect its last run:
1. Delete **Start the Apify run**, **Finished?**, **Failed?**, **Wait a minute**, **Check the run** and **Apify run failed**.
2. Point **Get the scraped jobs** at the URL below, and connect it straight after **Settings**.
3. Schedule n8n a little after Apify's run.

```
https://api.apify.com/v2/actor-tasks/<your-task-id>/runs/last/dataset/items?status=SUCCEEDED&clean=true
```

## The endpoint, for other tools

```
POST /api/import/jobs          (?fill=false to skip reading missing descriptions)
X-API-Key: <API_TOKEN>
X-User: Yazid                  (optional)
Content-Type: application/json

{"items": [ {...}, {...} ]}    (or just a JSON list)
```

Each item can use the scraper's own field names. Job Hacker recognises:
- link: `jobUrl`, `link`, `url`
- title: `title`, `jobTitle`, `positionName`
- company: `companyName`, `company` or `company.name`
- `location`
- description: `description`, `descriptionText` or `descriptionHtml`
- posted date: `publishedAt`, `postedAt` or `listedAt` (text dates or timestamps)
- salary: `salary` or `salaryInfo`
- job type: `employmentType`, `contractType` or `workType`
- Easy Apply: `applyType`, `easyApply`, or `applyMethod` with its apply URLs

Up to 5000 jobs per request.
