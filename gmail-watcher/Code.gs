/**
 * Gmail watcher (Google Apps Script, runs in Google's cloud, free).
 *
 * Every minute it looks for new job-alert emails, pulls out the job links and
 * posts them to your webhook (bot/server.py) with a secret key. That is all it
 * does: it never talks to Telegram and never writes to your mailbox.
 *
 * Setup (once):
 *   1. Create a new Apps Script project and paste this file in as Code.gs.
 *   2. Project Settings: tick "Show appsscript.json manifest file in editor",
 *      then replace that file with appsscript.json from this folder. It
 *      limits the script to READ-ONLY Gmail.
 *   3. Project Settings > Script properties, add:
 *        WEBHOOK_URL   the address of your webhook, ending in /jobs
 *        API_KEY       the same value as JAA_API_KEY on the webhook
 *   4. Run setup() once and accept the permissions.
 *
 * Gmail is read through the advanced Gmail service, and the only two calls
 * used are Users.Messages.list and Users.Messages.get. A test checks that no
 * other Gmail call appears in this file.
 *
 * What leaves Google: for each new job, its id, link, title, a short text
 * snippet (links removed) and a budget if one can be read from the snippet.
 * Only emails whose subject matches ALERT_SUBJECT are opened; every other email
 * is skipped after its subject line is read.
 */

var CONFIG = {
  QUERY: 'from:upwork.com newer_than:1d',
  ALERT_SUBJECT: /^\s*(?:(?:fwd?|re)\s*:\s*)*new job alert\b/i,
  MAX_MESSAGES: 30,
  MAX_JOBS_PER_POST: 10,
  SNIPPET_CHARS: 600,
  KEEP_DAYS: 3,
  // The email's own job link is used only if its host is on this list. Any other link, a lookalike host or a
  // tracking redirect to another site is ignored, and the link is built from the job id and JOB_URL_PREFIX instead.
  // The prefix must be an https address on one of these hosts.
  LINK_HOSTS: ['www.upwork.com', 'upwork.com'],
  JOB_URL_PREFIX: 'https://www.upwork.com/jobs/'
};

// ---------- pure helpers (tested in node by tests/watcher.test.js) ----------

function decodeEntities(s) {
  return s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;|&#x27;/g, "'").replace(/&nbsp;/g, ' ');
}

function safeDecode(s) {
  try { return decodeURIComponent(s); } catch (e) { return s; }
}

function stripTags(s) {
  return decodeEntities(s.replace(/<[^>]*>/g, ' ')).replace(/\s+/g, ' ').trim();
}

function isAlert(subject) {
  return CONFIG.ALERT_SUBJECT.test(subject || '');
}

/** The first dollar amount, or the highest one for an hourly range. null when none. */
function parseBudget(text, payment) {
  var nums = [], re = /\$\s?(\d[\d,]*(?:\.\d+)?)/g, m;
  while ((m = re.exec(text || '')) !== null) nums.push(parseFloat(m[1].replace(/,/g, '')));
  if (!nums.length) return null;
  return payment === 'hourly' ? Math.max.apply(null, nums) : nums[0];
}

/** 'hourly', 'fixed' or null, read from the words in the snippet. */
function parsePayment(text) {
  if (/\bhourly\b|\/\s?hr\b/i.test(text || '')) return 'hourly';
  if (/\bfixed[- ]?price\b|\bfixed\b/i.test(text || '')) return 'fixed';
  return null;
}

/** The host of an https address, lower case; '' unless it is plain https://host/... (no port, no user info, no odd characters). */
function hostOf(url) {
  var m = /^https:\/\/([A-Za-z0-9.-]+)(?:[\/?#]|$)/.exec(url || '');
  return m ? m[1].toLowerCase() : '';
}

/**
 * The link to show for a job. The email's own link (the https address that ends in the id, without its
 * query string) is used only when its host is in CONFIG.LINK_HOSTS. Otherwise the link is built from the id.
 */
function jobLink(href, id) {
  var m = /https:\/\/[^\s"'<>?#]*~0[12][0-9a-z]{12,24}/i.exec(href);
  if (m && CONFIG.LINK_HOSTS.indexOf(hostOf(m[0])) !== -1) return m[0];
  return CONFIG.JOB_URL_PREFIX + id;
}

/** Job ids (~01... or ~02...) in the email's links, with title and snippet, in order. */
function extractJobs(html) {
  html = html || '';
  var anchors = [], re = /<a\b[^>]*href\s*=\s*["']([^"']*)["'][^>]*>([\s\S]*?)<\/a>/gi, m;
  while ((m = re.exec(html)) !== null) {
    var href = safeDecode(safeDecode(decodeEntities(m[1])));
    var idm = /~0[12][0-9a-z]{12,24}/i.exec(href);
    if (idm) anchors.push({ id: idm[0].toLowerCase(), url: jobLink(href, idm[0].toLowerCase()), label: stripTags(m[2]), start: m.index });
  }
  var jobs = [], byId = {};
  anchors.forEach(function (a) {
    var job = byId[a.id];
    if (!job) {
      job = byId[a.id] = { id: a.id, url: a.url, title: '', start: a.start };
      jobs.push(job);
    }
    var generic = /^(view|apply|see|open|click)\b/i.test(a.label);
    if (a.label && a.label.length <= 200 && !generic && a.label.length > job.title.length) job.title = a.label;
  });
  jobs.forEach(function (job, i) {
    var end = i + 1 < jobs.length ? jobs[i + 1].start : html.length;
    var text = stripTags(html.slice(job.start, end)).replace(/https?:\/\/\S+/g, '').replace(/\s+/g, ' ').trim();
    job.description = text.slice(0, CONFIG.SNIPPET_CHARS);
    job.payment = parsePayment(text);
    job.budget = parseBudget(text, job.payment);
    if (!job.title) job.title = '(untitled job)';
    delete job.start;
  });
  return jobs;
}

/** Gmail returns base64url, sometimes without padding. */
function padB64(data) {
  return data + '===='.slice(0, (4 - data.length % 4) % 4);
}

function header(payload, name) {
  var hs = (payload && payload.headers) || [];
  for (var i = 0; i < hs.length; i++) if (hs[i].name.toLowerCase() === name.toLowerCase()) return hs[i].value;
  return '';
}

/** First body part of the given type, walking nested multipart parts. */
function findBody(part, mime, decode) {
  if (!part) return '';
  if (part.mimeType === mime && part.body && part.body.data) return decode(part.body.data);
  var parts = part.parts || [];
  for (var i = 0; i < parts.length; i++) {
    var b = findBody(parts[i], mime, decode);
    if (b) return b;
  }
  return '';
}

function parseMessage(m, decode) {
  var p = m.payload || {};
  var html = findBody(p, 'text/html', decode);
  return { id: m.id, subject: header(p, 'Subject'), time: Number(m.internalDate || 0), html: html || findBody(p, 'text/plain', decode) };
}

function chunk(list, size) {
  var out = [];
  for (var i = 0; i < list.length; i += size) out.push(list.slice(i, i + size));
  return out;
}

function buildPayload(jobs) {
  return JSON.stringify({ jobs: jobs });
}

// ---------- Google side ----------

function props() { return PropertiesService.getScriptProperties(); }

function decodeData_(data) {
  return Utilities.newBlob(Utilities.base64DecodeWebSafe(padB64(data))).getDataAsString('UTF-8');
}

function searchMessages_(query, max) {
  var res = Gmail.Users.Messages.list('me', { q: query, maxResults: max });
  return (res.messages || []).map(function (m) { return m.id; });
}

function readHeaders_(id) {
  return parseMessage(Gmail.Users.Messages.get('me', id, { format: 'metadata', metadataHeaders: ['Subject'] }), decodeData_);
}

function readMessage_(id) {
  return parseMessage(Gmail.Users.Messages.get('me', id, { format: 'full' }), decodeData_);
}

/** Posts jobs to the webhook with the key in a header. Never logs the key. */
function postJobs_(jobs) {
  var p = props(), url = p.getProperty('WEBHOOK_URL'), key = p.getProperty('API_KEY');
  if (!url || !key) return { ok: false, code: 0 };
  var r = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Api-Key': key },
    payload: buildPayload(jobs),
    muteHttpExceptions: true
  });
  var code = r.getResponseCode();
  return { ok: code >= 200 && code < 300, code: code };
}

/** Runs every minute. Only one run at a time. */
function tick() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try { run_(); } finally { lock.releaseLock(); }
}

function run_() {
  var p = props(), seen = p.getProperties(), now = String(Date.now());
  var batch = [], perMessage = {}, marks = {};

  searchMessages_(CONFIG.QUERY, CONFIG.MAX_MESSAGES).forEach(function (id) {
    var mid = 'm_' + id;
    if (seen[mid]) return;
    if (!isAlert(readHeaders_(id).subject)) { marks[mid] = now; return; }   // body never opened
    var fresh = extractJobs(readMessage_(id).html).filter(function (j) { return !seen['j_' + j.id]; });
    if (!fresh.length) { marks[mid] = now; return; }
    perMessage[mid] = fresh.map(function (j) { return j.id; });
    fresh.forEach(function (j) { if (!batch.some(function (b) { return b.id === j.id; })) batch.push(j); });
  });

  var posted = {};
  for (var i = 0, groups = chunk(batch, CONFIG.MAX_JOBS_PER_POST); i < groups.length; i++) {
    var res;
    try { res = postJobs_(groups[i]); } catch (e) { res = { ok: false, code: -1 }; }
    if (!res.ok) { Logger.log('webhook refused or unreachable (HTTP ' + res.code + '); will retry next minute'); break; }
    groups[i].forEach(function (j) { posted[j.id] = true; marks['j_' + j.id] = now; });
  }
  Object.keys(perMessage).forEach(function (mid) {
    if (perMessage[mid].every(function (id) { return posted[id]; })) marks[mid] = now;
  });
  if (Object.keys(marks).length) p.setProperties(marks);
  prune_(seen);
}

function prune_(seen) {
  var limit = Date.now() - CONFIG.KEEP_DAYS * 24 * 3600 * 1000, p = props();
  Object.keys(seen).forEach(function (k) {
    if ((k.indexOf('m_') === 0 || k.indexOf('j_') === 0) && Number(seen[k]) < limit) p.deleteProperty(k);
  });
}

// ---------- run these by hand ----------

/** Run once: installs the every-minute trigger and marks existing alerts as seen. */
function setup() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'tick') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('tick').timeBased().everyMinutes(1).create();
  var p = props();
  var missing = ['WEBHOOK_URL', 'API_KEY'].filter(function (k) { return !p.getProperty(k); });
  var marks = {}, now = String(Date.now());
  searchMessages_(CONFIG.QUERY, CONFIG.MAX_MESSAGES).forEach(function (id) {
    marks['m_' + id] = now;
    if (isAlert(readHeaders_(id).subject)) extractJobs(readMessage_(id).html).forEach(function (j) { marks['j_' + j.id] = now; });
  });
  if (Object.keys(marks).length) p.setProperties(marks);
  Logger.log(missing.length ? 'Installed, but these Script properties are missing: ' + missing.join(', ')
                            : 'Watching every minute. ' + Object.keys(marks).length + ' existing entries marked as seen.');
}

/** Logs how the latest alerts parse. Posts nothing. */
function diagnose() {
  searchMessages_(CONFIG.QUERY, 5).forEach(function (id) {
    var m = readHeaders_(id);
    var jobs = isAlert(m.subject) ? extractJobs(readMessage_(id).html) : [];
    Logger.log((isAlert(m.subject) ? m.subject + ': ' + jobs.length + ' job(s)' : 'skipped (not a job alert)'));
  });
}

/** Stops the watcher. */
function stop() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'tick') ScriptApp.deleteTrigger(t);
  });
}
