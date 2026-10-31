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
 *        WEBHOOK_URL   the https address of your webhook, ending in /jobs
 *        API_KEY       the same value as JAA_API_KEY on the webhook
 *   4. Run setup() once and accept the permissions.
 *
 * Gmail is read through the advanced Gmail service, and the only two calls
 * used are Users.Messages.list and Users.Messages.get. A test checks that no
 * other Gmail call appears in this file.
 *
 * What leaves Google: for each new job, its id, link, title, a short text
 * snippet (links removed), the payment type and a budget if one can be read
 * from the snippet. For an alert email with no job in it that can be read, its
 * subject line and Gmail message id, so the webhook can tell you about it.
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

// Named HTML codes: the Latin-1 set (&nbsp; to &yuml;, in code point order from 160) plus the common typographic ones.
var LATIN1_NAMES = ('nbsp iexcl cent pound curren yen brvbar sect uml copy ordf laquo not shy reg macr deg plusmn sup2 sup3 ' +
  'acute micro para middot cedil sup1 ordm raquo frac14 frac12 frac34 iquest Agrave Aacute Acirc Atilde Auml Aring AElig ' +
  'Ccedil Egrave Eacute Ecirc Euml Igrave Iacute Icirc Iuml ETH Ntilde Ograve Oacute Ocirc Otilde Ouml times Oslash Ugrave ' +
  'Uacute Ucirc Uuml Yacute THORN szlig agrave aacute acirc atilde auml aring aelig ccedil egrave eacute ecirc euml igrave ' +
  'iacute icirc iuml eth ntilde ograve oacute ocirc otilde ouml divide oslash ugrave uacute ucirc uuml yacute thorn yuml').split(' ');
var NAMED_CODES = (function () {
  var map = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", ndash: '–', mdash: '—', lsquo: '‘',
              rsquo: '’', sbquo: '‚', ldquo: '“', rdquo: '”', bdquo: '„', bull: '•',
              hellip: '…', trade: '™', euro: '€', lsaquo: '‹', rsaquo: '›', dagger: '†',
              Dagger: '‡', permil: '‰', OElig: 'Œ', oelig: 'œ', Scaron: 'Š', scaron: 'š',
              Yuml: 'Ÿ', fnof: 'ƒ', circ: 'ˆ', tilde: '˜', ensp: ' ', emsp: ' ', thinsp: ' ',
              zwnj: '', zwj: '', lrm: '', rlm: '' };
  LATIN1_NAMES.forEach(function (name, i) { map[name] = String.fromCharCode(160 + i); });
  map.nbsp = ' ';
  map.shy = '';
  return map;
})();

/** Turns every HTML code (&amp;, &rsquo;, &#8217;, &#x2019; ...) into its character, in one pass, so "&amp;lt;" stays "&lt;". */
function decodeEntities(s) {
  return s.replace(/&(#\d{1,7}|#x[0-9a-f]{1,6}|[a-z][a-z0-9]{1,31});/gi, function (whole, code) {
    if (code.charAt(0) === '#') {
      var n = code.charAt(1) === 'x' || code.charAt(1) === 'X' ? parseInt(code.slice(2), 16) : parseInt(code.slice(1), 10);
      return n > 0 && n <= 0x10ffff && (n < 0xd800 || n > 0xdfff) ? String.fromCodePoint(n) : whole;
    }
    if (Object.prototype.hasOwnProperty.call(NAMED_CODES, code)) return NAMED_CODES[code];
    var lower = code.toLowerCase();
    return Object.prototype.hasOwnProperty.call(NAMED_CODES, lower) ? NAMED_CODES[lower] : whole;
  });
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

/** The job title in a subject such as "New job alert: Clean up spreadsheets"; '' when there is none. */
function subjectTitle(subject) {
  var m = /new job alert\s*[:\-–—]\s*(.+)$/i.exec(subject || '');
  return m ? m[1].replace(/\s+/g, ' ').trim().slice(0, 200) : '';
}

// A budget is read only right after one of these labels. Client lines such as "$50K+ spent" have no label.
var BUDGET_RE = new RegExp('\\b(?:est(?:imated)?\\.?\\s*budget|budget|fixed[- ]?price|hourly(?:\\s*rate)?)\\s*[:\\-\\u2013\\u2014]?\\s*' +
  '\\$\\s?(\\d[\\d,]*(?:\\.\\d+)?)\\s?([kK](?![a-z]))?' +
  '(?:\\s*(?:-|\\u2013|\\u2014|to)\\s*\\$\\s?(\\d[\\d,]*(?:\\.\\d+)?)\\s?([kK](?![a-z]))?)?', 'i');

function amount_(digits, k) {
  var n = parseFloat(digits.replace(/,/g, ''));
  return k ? n * 1000 : n;
}

/**
 * The amount after a budget label (Budget, Est. budget, Fixed-price, Fixed price, Hourly, Hourly rate), or null.
 * For an hourly range the upper end is used, for anything else the first amount.
 */
function parseBudget(text, payment) {
  var m = BUDGET_RE.exec(text || '');
  if (!m) return null;
  var first = amount_(m[1], m[2]);
  return payment === 'hourly' && m[3] ? Math.max(first, amount_(m[3], m[4])) : first;
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

var ID_RE = /~0[12][0-9a-z]{12,24}/i;

/** Every job id in the email, in the raw text and inside links (also when a tracker hides it as %7E...). */
function findIds_(html) {
  var ids = [], seen = {};
  var texts = [html].concat((decodeEntities(html).match(/https?:\/\/[^\s"'<>]+|%7e0[12][0-9a-z]{12,24}/gi) || [])
    .map(function (u) { return safeDecode(safeDecode(u)); }));
  texts.forEach(function (t) {
    var re = new RegExp(ID_RE.source, 'gi'), m;
    while ((m = re.exec(t)) !== null) {
      var id = m[0].toLowerCase();
      if (!seen[id]) { seen[id] = true; ids.push(id); }
    }
  });
  return ids;
}

/** The first line of text that can be a title: not a link, not a pay line, not too long. */
function firstLine_(htmlPart) {
  var text = decodeEntities(htmlPart.replace(/<(?:br|\/p|\/div|\/tr|\/td|\/li|\/h\d)\b[^>]*>/gi, '\n').replace(/<[^>]*>/g, ' '));
  var lines = text.split('\n').map(function (l) { return l.replace(/\s+/g, ' ').trim(); });
  for (var i = 0; i < lines.length; i++) {
    var l = lines[i];
    if (l && l.length <= 200 && !/https?:\/\//i.test(l) && !/^(?:est\.?\s*budget|budget|fixed|hourly|\$)/i.test(l)) return l;
  }
  return '';
}

/** The token (a link, usually) around position at, decoded, so jobLink can read it. */
function tokenAt_(html, at, from) {
  var left = html.slice(from, at), cut = Math.max(left.search(/[^\s"'<>]*$/), 0);
  var right = /^[^\s"'<>]*/.exec(html.slice(at))[0];
  return safeDecode(safeDecode(decodeEntities(left.slice(cut) + right)));
}

/**
 * Jobs (~01... or ~02... ids) found anywhere in the email, with link, title and snippet, in order.
 * A job link in an <a> tag gives the title (its longest text that is not a "View job" button) and starts the
 * job's snippet. An id found only outside a tag (a plain-text email, a bare link) ends its block instead:
 * plain-text alerts list the title and the pay line first and the link last.
 */
function extractJobs(html, subject) {
  html = html || '';
  var lower = html.toLowerCase(), byId = {}, jobs = [];
  findIds_(html).forEach(function (id) {
    var at = Math.max(lower.indexOf(id.slice(1)), 0);   // the digits are in the text whether the ~ is written or encoded
    jobs.push(byId[id] = { id: id, url: '', title: '', start: at, end: at + id.length - 1, anchored: false });
  });
  var re = /<a\b[^>]*href\s*=\s*["']([^"']*)["'][^>]*>([\s\S]*?)<\/a>/gi, m;
  while ((m = re.exec(html)) !== null) {
    var href = safeDecode(safeDecode(decodeEntities(m[1])));
    var idm = ID_RE.exec(href), job = idm && byId[idm[0].toLowerCase()];
    if (!job) continue;
    if (!job.anchored) {   // the first link that carries the id gives the job its link and its place
      job.anchored = true;
      job.url = jobLink(href, job.id);
      job.start = m.index;
    }
    var label = stripTags(m[2]), generic = /^(view|apply|see|open|click)\b/i.test(label);
    if (label && label.length <= 200 && !generic && label.length > job.title.length) job.title = label;
  }
  jobs.sort(function (a, b) { return a.start - b.start; });
  var blockEnd = 0;
  jobs.forEach(function (job, i) {
    var from = job.anchored ? job.start : blockEnd;
    var to = job.anchored ? (i + 1 < jobs.length ? jobs[i + 1].start : html.length) : job.end;
    if (!job.anchored) {
      job.url = jobLink(tokenAt_(html, job.start, from), job.id);
      job.title = firstLine_(html.slice(from, job.start));
    }
    blockEnd = to;
    var text = stripTags(html.slice(from, to)).replace(/https?:\/\/\S+/g, '').replace(/\s+/g, ' ').trim();
    job.description = text.slice(0, CONFIG.SNIPPET_CHARS);
    job.payment = parsePayment(text);
    job.budget = parseBudget(text, job.payment);
  });
  // One job in the email: the subject names it ("New job alert: <title>"), the safest title when no link text does.
  var fromSubject = jobs.length === 1 ? subjectTitle(subject) : '';
  jobs.forEach(function (job) {
    if (fromSubject && (!job.title || !job.anchored)) job.title = fromSubject;
    if (!job.title) job.title = '(untitled job)';
    delete job.start; delete job.end; delete job.anchored;
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

/** jobs: the jobs to post. unreadable: alert emails with no job in them, as {id, subject}. */
function buildPayload(jobs, unreadable) {
  var body = { jobs: jobs };
  if (unreadable && unreadable.length) {
    body.unreadable = unreadable.map(function (u) { return { id: u.id, subject: String(u.subject || '').slice(0, 200) }; });
  }
  return JSON.stringify(body);
}

function isHttps_(url) {
  return /^https:\/\//i.test(url || '');
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

/** Posts a payload to the webhook with the key in a header, only over https. Never logs the key. */
function postJobs_(payload) {
  var p = props(), url = p.getProperty('WEBHOOK_URL'), key = p.getProperty('API_KEY');
  if (!url || !key) return { ok: false, code: 0 };
  if (!isHttps_(url)) {
    Logger.log('WEBHOOK_URL must start with https://, so the key is never sent unencrypted. Nothing was posted.');
    return { ok: false, code: 0 };
  }
  var r = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: { 'X-Api-Key': key },
    payload: payload,
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
  var batch = [], perMessage = {}, marks = {}, unreadable = [];

  searchMessages_(CONFIG.QUERY, CONFIG.MAX_MESSAGES).forEach(function (id) {
    var mid = 'm_' + id;
    if (seen[mid]) return;
    if (!isAlert(readHeaders_(id).subject)) { marks[mid] = now; return; }   // body never opened
    var msg = readMessage_(id), found = extractJobs(msg.html, msg.subject);
    if (!found.length) {
      // Never marked read in silence: the webhook posts a short note, and only then is the email marked.
      Logger.log('No job could be read from the alert "' + msg.subject + '". A note goes to Telegram.');
      unreadable.push({ id: id, subject: msg.subject });
      return;
    }
    var fresh = found.filter(function (j) { return !seen['j_' + j.id]; });
    if (!fresh.length) { marks[mid] = now; return; }
    perMessage[mid] = fresh.map(function (j) { return j.id; });
    fresh.forEach(function (j) { if (!batch.some(function (b) { return b.id === j.id; })) batch.push(j); });
  });

  var posted = {};
  var posts = chunk(batch, CONFIG.MAX_JOBS_PER_POST).map(function (g) { return { jobs: g, unreadable: [] }; })
    .concat(chunk(unreadable, CONFIG.MAX_JOBS_PER_POST).map(function (g) { return { jobs: [], unreadable: g }; }));
  for (var i = 0; i < posts.length; i++) {
    var res;
    try { res = postJobs_(buildPayload(posts[i].jobs, posts[i].unreadable)); } catch (e) { res = { ok: false, code: -1 }; }
    if (!res.ok) { Logger.log('webhook refused or unreachable (HTTP ' + res.code + '); will retry next minute'); break; }
    posts[i].jobs.forEach(function (j) { posted[j.id] = true; marks['j_' + j.id] = now; });
    posts[i].unreadable.forEach(function (u) { marks['m_' + u.id] = now; });
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
  if (p.getProperty('WEBHOOK_URL') && !isHttps_(p.getProperty('WEBHOOK_URL'))) missing.push('WEBHOOK_URL (it must start with https://)');
  var marks = {}, now = String(Date.now());
  searchMessages_(CONFIG.QUERY, CONFIG.MAX_MESSAGES).forEach(function (id) {
    marks['m_' + id] = now;
    if (isAlert(readHeaders_(id).subject)) {
      var msg = readMessage_(id);
      extractJobs(msg.html, msg.subject).forEach(function (j) { marks['j_' + j.id] = now; });
    }
  });
  if (Object.keys(marks).length) p.setProperties(marks);
  Logger.log(missing.length ? 'Installed, but these Script properties are missing or wrong: ' + missing.join(', ')
                            : 'Watching every minute. ' + Object.keys(marks).length + ' existing entries marked as seen.');
}

/** Logs how the latest alerts parse: every job with its payment type, budget and title. Posts nothing. */
function diagnose() {
  searchMessages_(CONFIG.QUERY, 5).forEach(function (id) {
    var m = readHeaders_(id);
    if (!isAlert(m.subject)) { Logger.log('skipped (not a job alert)'); return; }
    var msg = readMessage_(id), jobs = extractJobs(msg.html, msg.subject);
    Logger.log(m.subject + ': ' + jobs.length + ' job(s)' + (jobs.length ? '' : ', so a note would go to Telegram'));
    jobs.forEach(function (j) {
      Logger.log('  ' + j.id + '  ' + (j.payment || 'payment ?') + '  ' + (j.budget === null ? 'budget ?' : '$' + j.budget) + '  ' + j.title);
    });
  });
}

/** Stops the watcher. */
function stop() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'tick') ScriptApp.deleteTrigger(t);
  });
}
