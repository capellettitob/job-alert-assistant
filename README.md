# job-alert-assistant

Job alert emails in Gmail, a rule check against your profile, and a Telegram ping with a saved draft and two buttons: **Go** and **Skip**. Optionally, an AI model also checks whether you can deliver the job and writes a tailored draft (see [AI steps](#ai-steps-optional-off-by-default)).

A person reads every job and decides. Nothing here applies to a job for you. Go posts the job link and the saved draft back to your chat, and you submit it yourself.

This project is not affiliated with, endorsed by or connected to Upwork, Telegram or Google. It only reads email in your own mailbox and talks to your own Telegram bot (and, if you switch the AI steps on, to the Anthropic API with your own key). You are responsible for following the terms and rules of every platform you use it with, including how you reply to and apply for jobs and any rules about AI-assisted writing.

## How it works

```
 job alert email in Gmail
          |
          v
 gmail-watcher/Code.gs          Google Apps Script, runs every minute, READ-ONLY Gmail
   pulls job ids, titles, snippets (an alert with no job in it becomes a short note)
          |   HTTPS POST + secret key (X-Api-Key header)
          v
 bot/server.py                  Python, standard library only
   1. checks the key
   2. checks every field of every job (including that the link points to an allowed host)
   3. skips jobs it has already seen
   4. checks the job type and the budget (both are gates) and scores the job
      with bot/rules.py and your config.json
   5. OPTIONAL, off by default (bot/llm.py), only for jobs that passed step 4:
        a. feasibility check  -> under min_confidence the job is skipped
        b. tailored draft     -> falls back to the template if it fails or breaks the rules
   6. saves the draft, then builds the message with bot/message.py (everything escaped)
          |
          v
 Telegram message: job, checks, score, [AI check], saved draft    [ Go ]  [ Skip ]
          |
          |  you tap a button, Telegram calls the webhook
          v
 Go    posts the job link and the saved draft to your chat
 Skip  closes the job
```

You read the job post, edit the draft if you want, and submit it yourself on the job site.

## Sample pings

Both are output of `python3 demo.py` for invented jobs. The demo prints plain text; Telegram shows the same content with formatting.

**AI steps off (the default):** a keyword checklist and a fixed draft template.

```
New job: Clean up and merge three sales spreadsheets
Score: 100 out of 100
Matches your profile: spreadsheet, data cleanup, dashboard

Job type: pass, matches spreadsheet, data cleanup, dashboard
Budget: pass, 250 fixed reaches your minimum of 100
Competition: pass, 4 proposals so far, your limit is 15
Client rating: unknown, client rating not in the post

Saved draft
    Hello,

    I read your post "Clean up and merge three sales spreadsheets" and the work looks close to what I do: spreadsheet, data cleanup, dashboard.

    Tell me what the finished result should look like and when you need it, and I will confirm the plan and a price.

    Thanks

Open the job post: https://example.com/jobs/~01demojobalpha01

Buttons: [ Go ]   [ Skip ]
Go posts the job link and the draft again. Skip closes the job.
```

**AI steps on, with the FAKE model.** The demo's model is a stand-in with scripted answers: no network call, nothing is assessed. It shows the layout. The first job passes the AI check and gets an AI draft; the sample disclosure line from `config.example.json` is its last line:

```
New job: Clean up and merge three sales spreadsheets
Score: 100 out of 100
Matches your profile: spreadsheet, data cleanup, dashboard

Job type: pass, matches spreadsheet, data cleanup, dashboard
Budget: pass, 250 fixed reaches your minimum of 100
Competition: pass, 4 proposals so far, your limit is 15
Client rating: unknown, client rating not in the post

AI check: confidence 97 out of 100, can deliver: yes
(the confidence is the model's own estimate, not a measured accuracy)
AI reason: Spreadsheet merging and a summary dashboard match two portfolio entries.
AI risks: Date formats may need a decision from the client.
Model: FAKE MODEL (canned or scripted answers, nothing is really assessed)

Saved draft (written by the AI step, read it before you use it)
    Hello, I read your post about merging three regional sales spreadsheets with mixed date formats. My portfolio has a customer list cleanup that merged three sheets into one deduplicated sheet, and a weekly sales dashboard built from CSV exports. Tell me what the summary should show and I will confirm the plan. (Scripted text from the fake model.)

    AI helped me draft this reply. I read and check every detail myself.

Open the job post: https://example.com/jobs/~01demojobalpha01

Buttons: [ Go ]   [ Skip ]
Go posts the job link and the draft again. Skip closes the job.
```

A job that passes the keyword rules but not the AI check gets no ping. A short silent note is posted to Telegram instead (this is what the demo prints for it):

```
No ping. A silent note is posted to Telegram instead, with no buttons:
Skipped: Python dashboard plus a native mobile app with payments. AI check: the model says it cannot deliver this (confidence 40). The dashboard fits the portfolio but a native mobile app with card payments does not.
```

## Try it with no accounts

You need Python 3.9 or newer. Node is optional (only for the Apps Script tests).

```
python3 demo.py                 # sample pings, AI off and then AI on with the fake model
python3 -m unittest discover -s tests -t . -v    # all tests, one command
python3 -m bot score tests/sample_job.json --config config.example.json
```

Everything in the tests and the demo is invented: jobs, ids, names and links (the links use example.com, or an allowed host with an invented id). The tests never call a real model.

## The vetting rules

`config.example.json` holds SAMPLE values. Copy it to `config.json` and change them to match you.

| Check | Passes when | Sample value |
| --- | --- | --- |
| job_type | the title or description mentions enough of your skills, and none of the words you avoid. This check is a gate: without it there is never a ping. | 1 of 5 sample skills |
| budget | the budget reaches your minimum for that payment type. Also a gate: a known budget under your minimum means no ping. An unknown budget does not block. | 100 fixed, 20 an hour |
| competition | no more than this many proposals so far | 15 |
| client_rating | the client's rating reaches this | 4.5 |

Each check has a weight. The score is the weight of the passed checks divided by the weight of the checks that could be judged. A job gets a ping when the job type passes, the budget is not under your minimum, and the score reaches `min_score_to_ping` (sample: 0.6).

A check that the post gives no data for is shown as **unknown**. It is left out of the score instead of counting against the job, and the ping says so, so you can look it up yourself. Job alert emails often leave out the proposal count and the client rating, so expect those to be unknown unless something else fills them in.

The watcher reads a budget only right after a budget label (Budget, Est. budget, Fixed-price, Hourly), never from a client line such as "$50K+ spent". For an hourly range it takes the upper end. The payment type comes from the words fixed or hourly; with neither, the budget check is unknown.

The draft is a template (`draft_template` in the config) with two placeholders, `$title` and `$skills`. Write your own.

**Job links.** `allowed_link_hosts` (optional; default `www.upwork.com` and `upwork.com`) lists the hosts a job link may point to. The webhook refuses any job whose `url` is not a plain `https://host/...` link on one of those hosts: no port, no `user@host`, no lookalike such as `upwork.com.evil.example`, and a tracking-redirect link is refused because its host is the tracker's. This is the second lock behind the watcher's own list, `CONFIG.LINK_HOSTS` in `Code.gs` (see Security notes). Change both if your alerts come from another site.

## AI steps (optional, off by default)

Off unless `llm.enabled` is `true` in `config.json`. With them off, the AI code is never called (only its settings are checked at start), no model is contacted, and everything behaves as described above: the keyword checklist and the template draft.

When on, they run only for a job that already passed the keyword rules (the job type and budget gates and the score threshold). A job those rules refused never reaches the model.

1. **Feasibility check.** The model is asked whether you can deliver the job, using only your skills and your portfolio entries as evidence. It must answer strict JSON: `can_deliver` (true or false), `confidence` (a whole number from 0 to 100), `reason` and `risks`. If it says it cannot deliver, or the confidence is below `min_confidence` (default 95), the job is skipped: no ping, and a short silent Telegram note gives the reason. Exactly at the limit passes (95 passes, 94 skips).
2. **Tailored draft.** For a job that passed the check, the model writes a short proposal from the job and your profile. It is told not to invent experience and not to state a price, rate, date or guarantee. The code then checks the text and refuses it (and uses your template instead) if it is shorter than 40 or longer than `draft_max_chars` characters (default 900), or contains a link or web address, an email address, a phone number, a price or rate, a date or time promise, a guarantee, a claim of years of experience, markdown or markup, or a mention of being an AI. The code cannot check that the text claims no experience you lack; the prompt tells the model not to, and you read the draft before you use it.

**If the model fails, nothing is blocked.** A failed call, a timeout, a refusal, an answer that is not valid JSON, or an answer with a wrong type or range makes the AI check "unknown". The ping goes out anyway, says "AI check: unknown" and why, and uses the template draft. A broken draft also falls back to the template.

**What is sent to the model.** Each call sends a fixed instruction text, your skills list, your portfolio entries (title and summary from `config.json`), and the job's title and text as they appear in the alert email. That text can include the budget and the client line (for example "Payment verified, $1K+ spent"), because the email shows them next to the job. The job text has links removed, `<` and `>` swapped for look-alike quote marks, and is cut to 200 characters (title) and 1,200 characters (text). Nothing else is sent: not the job id or link, the separate budget, proposal count and rating fields, your Telegram details, your email or any key. The key goes only in the request header to `api.anthropic.com`.

**Limits and costs.**
- `max_llm_calls_per_day` (default 40) caps model calls per UTC day. A call counts when it starts, whether or not it works. The count is kept in a small file next to `state.json`, so it survives restarts.
- Every call has a hard timeout (`timeout_seconds`: 25 in the example config, 10 if you leave it out, 30 at most). The server stops waiting after that. There are no retries. A call that times out may still be billed.
- One webhook request stops starting new model calls 40 seconds after it began, so a large batch cannot stall the watcher for long (a call already running still finishes or times out on its own, so one request takes about a minute at most in normal use; Apps Script waits up to 6 minutes). Jobs after that get "AI check: unknown" and the template.
- If Telegram is down when a ping is sent, the draft and the AI result are saved first and reused when the watcher retries, so an outage costs no extra model calls.
- Model calls never hold up the buttons: a tap is answered while the model is still working, and the same job is never worked on twice at once.
- Calls are billed to your Anthropic account. The cost per job has not been measured.
- The default model id is `claude-opus-5-5` (`llm.model`). Change it if you prefer another. The request uses structured output and low effort; a model that does not accept those settings may return an error, which shows up as "AI check: unknown".

**The confidence number is the model's own self-report.** It is not a measured accuracy and it has not been checked against real outcomes. The ping says so. A model can be wrong or overconfident, so the check narrows what you read; it does not replace reading it.

**AI disclosure line (optional).** `llm.disclosure_line` is one line of plain text (at most 200 characters; empty means off, and empty is the code default, but the example config sets the sample line, so clear it there if you do not want it) that is added as the last line of every draft the AI step wrote, after the draft rules have been checked. It is therefore in the stored draft and in the Go message too. It is never added to your template draft: you write that one yourself. The line is added locally and is not sent to the model. `config.example.json` holds a sample ("AI helped me draft this reply. I read and check every detail myself."). What you must disclose depends on the platform and the client, so check that yourself.

**A human still reads every job.** The AI steps can skip a job and write a draft. They never reply to a client or apply for anything. The only things they write are the ping and the silent skip note in your own Telegram chat, apart from the requests to the model itself. You open the job, edit the draft and submit it yourself.

**The job text is treated as untrusted.** It sits between `<job_post>` tags, and the instructions tell the model that this text is data and never instructions. The answer is validated strictly before it is used, and anything the model wrote is escaped again before it goes into a Telegram message. This reduces the risk of prompt injection; it cannot remove it. A model that is fooled into a high confidence still only produces a ping that you read, and it can never revive a job the keyword rules refused.

**Turning it on.**
1. In `config.json` set `llm.enabled` to `true`, replace the sample `llm.portfolio` entries with short true descriptions of your own work, and decide whether to keep, change or clear `llm.disclosure_line`.
2. Put `ANTHROPIC_API_KEY` in your environment (see `.env.example`). The server refuses to start if the AI steps are on and the key is missing.
3. Optional: `python3 -m bot llm-check` explains what it would do. With `--yes` it makes up to two real, paid calls to the model with one invented job, to test your key and setup. It changes no files and does not touch your daily count. The project's tests check the request and the answer handling against a stand-in model, never the real one; `llm-check --yes` is the live check, with your own key. Running it as `time python3 -m bot llm-check --yes` also shows whether `timeout_seconds` is long enough.

`llm.provider` can also be `"fake"`: canned answers labelled FAKE MODEL, for trying the pipeline offline.

## Setup

The webhook needs a computer that is always on: a small cloud server, a Raspberry Pi, or a home computer that stays on, with Python 3.9 or newer. The Gmail watcher runs in Google's cloud and needs nothing from you once it is set up.

1. **Telegram bot.** Talk to @BotFather in Telegram, create a bot and keep the token. Write any message to your new bot, then open `https://api.telegram.org/bot<token>/getUpdates` in a browser, with your token in place of `<token>`. The number after `"chat":{"id":` is your chat id. Do this before step 4: once the webhook is set, `getUpdates` stops working.
2. **Config and secrets.**
   ```
   cp config.example.json config.json      # edit the skills, minimums and draft
   cp .env.example .env                    # fill in the four values
   chmod 600 .env                          # only you can read the keys
   set -a; . ./.env; set +a
   python3 -m bot check
   ```
   Make the two keys long and random, for example `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`. `check` reads the config and the environment, asks Telegram who the bot is and sends one test message to your chat, so a wrong token or chat id shows up here and not later.
3. **Run the webhook.** `python3 -m bot serve` listens on `127.0.0.1:8080`. To keep it running after you close the terminal, start it with `nohup python3 -m bot serve >> bot.log 2>&1 &` in the shell where you loaded `.env`, or use a service manager (systemd on Linux, launchd on a Mac) so it also comes back after a reboot; with systemd, `EnvironmentFile=/path/to/.env` loads the keys.

   Apps Script and Telegram both need to reach it over public HTTPS, and the server itself does not do TLS. Two ways:
   - **A tunnel**, with nothing to open on your router. With Cloudflare's free `cloudflared`, `cloudflared tunnel --url http://127.0.0.1:8080` prints an `https://...trycloudflare.com` address. That quick address changes each time the tunnel starts, so for lasting use set up a named tunnel with a fixed host name.
   - **A reverse proxy** on a server with a domain name. Caddy gets the certificate by itself: `caddy reverse-proxy --from your-host.example --to 127.0.0.1:8080`.

   Telegram delivers webhooks only to ports 443, 80, 88 and 8443. Both ways above use 443, the normal HTTPS port.
4. **Tell Telegram where it is.** `python3 -m bot set-webhook https://your-host.example/telegram`
5. **Gmail watcher.** In the Google Apps Script editor create a project, paste in `gmail-watcher/Code.gs`, and replace its manifest with `gmail-watcher/appsscript.json` (Project Settings, tick "Show appsscript.json manifest file in editor"). Add two Script properties: `WEBHOOK_URL` (your https address ending in `/jobs`; an http address is refused) and `API_KEY` (the same value as `JAA_API_KEY`). Run `setup()` once and accept the permissions. Google then shows "Google hasn't verified this app", because the script is your own and not a published app: click **Advanced**, then **Go to** your project's name **(unsafe)**, then **Allow**. Run `diagnose()` to see in the log how your latest alerts parse: every job with its payment type, budget and title.
6. **Check it.** Post a job by hand:
   ```
   curl -X POST https://your-host.example/jobs \
     -H "X-Api-Key: $JAA_API_KEY" -H "Content-Type: application/json" \
     -d @tests/sample_job_post.json
   ```
   A ping should arrive in Telegram. Posting the same job again does nothing, because each job id is only pinged once. If the AI steps are on, this makes real, paid model calls (at most two for this one job).

The Gmail query, the subject pattern for alert emails and the allowed link hosts (`LINK_HOSTS`, `JOB_URL_PREFIX`) are at the top of `Code.gs` (`CONFIG`). Change them if your alerts look different, and keep `allowed_link_hosts` in `config.json` in step.

## Security notes

- **Read-only Gmail.** The manifest asks for the `gmail.readonly` scope and nothing wider. The script uses only the Gmail calls `Users.Messages.list` and `Users.Messages.get`; a test fails if any other Gmail call appears in `Code.gs`, or if the manifest asks for a wider scope. The watcher is not a web app, so nothing can call it from outside.
- **Only alert emails are opened.** Every other email is skipped after its subject line is read. An alert that yields no job is never marked read in silence: the watcher logs it, and the webhook posts a short silent Telegram note with its subject, once per email.
- **What leaves Google.** For each new job: its id, link (the email's own link if its host is allowed, otherwise a link built from the id), title, a snippet of up to 600 characters with links removed, the payment type (fixed or hourly) and a budget if one follows a budget label. For an alert with no readable job: its subject line and its Gmail message id. It goes only to the address in `WEBHOOK_URL`, which must be https: the watcher refuses an http address, so the key never travels unencrypted.
- **Job links.** The watcher uses the link from an email only if its host is in `CONFIG.LINK_HOSTS` (default `www.upwork.com` and `upwork.com`); a link on any other host (including lookalikes such as `upwork.com.evil.example`) and any tracking redirect that hides another host is replaced by a link built from the job id and `CONFIG.JOB_URL_PREFIX` (a redirect that hides an allowed host gives the clean allowed link, never the tracker), so a fake alert email cannot put a phishing link behind "Open the job post". The webhook checks again and refuses a job whose link host is not in `allowed_link_hosts`.
- **Key-protected webhook.** `/jobs` needs the `X-Api-Key` header and `/telegram` needs Telegram's secret-token header. They are two different secrets, compared in constant time, and checked before the request body is read. A key in the address does not count. Request bodies over 64 KB, and bodies nested too deeply to read, are refused. A client that stalls for 10 seconds while sending its request is cut off.
- **Only your chat.** Button taps from any other person or chat are refused.
- **Escaping.** Job titles, descriptions, model answers and drafts come from outside, so every field is escaped before it goes into a Telegram message, and every part is cut to length after escaping.
- **Secrets live in environment variables or Apps Script Script properties.** Nothing secret is in the code, the config example or the git history. `.env`, `config.json` and `state.json` are in `.gitignore`, and `.env.example` holds placeholders only. Error messages, including the model errors, never include the bot token or any key.
- **Fail loudly.** The server refuses to start with a missing or short key, or with the AI steps on and no Anthropic key. `python3 -m bot check` fails if Telegram does not accept the token or the chat id.

## Limits

- Alert email layouts change. The parser looks for job ids anywhere in the email and reads payment and budget from the text near them, on a best-effort basis. Run `diagnose()` and adjust `CONFIG` if your alerts parse badly.
- The every-minute trigger counts against Apps Script's daily trigger runtime: 90 minutes a day on a free Google account, 6 hours on Google Workspace. A normal run takes a few seconds; a run that waits for the AI steps can take about a minute. If the day's runtime runs out, runs fail until the next day.
- One person, one chat. State is a JSON file (`state.json`), which is fine for this size.
- The score is a plain checklist, not a prediction of anything. Use your own judgement.
- The tests run the AI steps against a stand-in model only. `python3 -m bot llm-check --yes` is the live check with your own key; see "Turning it on" above.

## Files

```
README.md  LICENSE  .env.example  .gitignore
config.example.json        sample profile, rules, allowed link hosts, AI settings and draft template
demo.py                    prints sample pings (AI off, then AI on with the fake model), no accounts needed
gmail-watcher/Code.gs      Google Apps Script watcher
gmail-watcher/appsscript.json   manifest with the read-only scope
bot/rules.py               job checks and scoring
bot/llm.py                 optional AI steps: providers, prompts, strict answer checks, limits
bot/message.py             builds and escapes the Telegram messages and buttons
bot/server.py              webhook: jobs in, buttons handled
bot/telegram.py            tiny Bot API client
bot/store.py               JSON file that remembers seen jobs
bot/settings.py            reads secrets from the environment
tests/                     Python tests (test_llm.py covers the AI steps), plus watcher.test.js for the Apps Script
```

## License

MIT, see [LICENSE](LICENSE).
