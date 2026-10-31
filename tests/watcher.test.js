// Tests for gmail-watcher/Code.gs. Run with:  node tests/watcher.test.js
// The script is loaded into a sandbox with a fake Gmail, fake web calls and
// fake script properties, so nothing here touches a real account or network.
// All emails, jobs and ids below are invented.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', 'gmail-watcher', 'Code.gs'), 'utf8');
const MANIFEST = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'gmail-watcher', 'appsscript.json'), 'utf8'));

const KEY = 'example-api-key-for-tests';
const HOOK = 'https://hooks.example.com/jobs';

const B64 = (s) => Buffer.from(s, 'utf8').toString('base64url');

function emailMessage(id, subject, html) {
  return {
    id: id,
    internalDate: String(Date.now()),
    payload: {
      headers: [{ name: 'Subject', value: subject }],
      mimeType: 'multipart/alternative',
      parts: [{ mimeType: 'text/html', body: { data: B64(html) } }]
    }
  };
}

const ALERT_HTML = [
  '<html><body><h1>New jobs for you</h1>',
  '<div><a href="https://www.upwork.com/jobs/Clean-up-spreadsheets_~01demoaaaaaaaaaa1/?referrer=email">Clean up three spreadsheets</a>',
  '<p>Fixed price: $1,250. Need data cleanup.</p>',
  '<a href="https://www.upwork.com/jobs/~01demoaaaaaaaaaa1">View job</a></div>',
  '<div><a href="https://track.example.com/click?u=https%3A%2F%2Fwww.upwork.com%2Fjobs%2F~02demobbbbbbbbbb02">Weekly report builder</a>',
  '<p>Hourly: $20.00 - $35.00 / hr. See https://example.com/more for details.</p></div>',
  '</body></html>'
].join('\n');

/** A fresh sandbox with the script loaded and every Google service faked. */
function sandbox(messages, opts) {
  opts = opts || {};
  const store = Object.assign({}, opts.props === undefined ? { WEBHOOK_URL: HOOK, API_KEY: KEY } : opts.props);
  const calls = { gmail: [], fetch: [], log: [] };
  let statuses = (opts.statuses || []).slice();
  const ctx = {
    console: console,
    Logger: { log: (m) => calls.log.push(String(m)) },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperties: () => Object.assign({}, store),
        getProperty: (k) => (k in store ? store[k] : null),
        setProperty: (k, v) => { store[k] = String(v); },
        setProperties: (o) => Object.keys(o).forEach((k) => { store[k] = String(o[k]); }),
        deleteProperty: (k) => { delete store[k]; }
      })
    },
    LockService: { getScriptLock: () => ({ tryLock: () => true, releaseLock: () => {} }) },
    Utilities: {
      base64DecodeWebSafe: (s) => Buffer.from(s, 'base64'),
      newBlob: (bytes) => ({ getDataAsString: () => Buffer.from(bytes).toString('utf8') })
    },
    Gmail: {
      Users: {
        Messages: {
          list: (user, o) => { calls.gmail.push('list'); return { messages: messages.map((m) => ({ id: m.id })) }; },
          get: (user, id, o) => {
            calls.gmail.push('get:' + o.format + ':' + id);
            return messages.find((m) => m.id === id);
          }
        }
      }
    },
    UrlFetchApp: {
      fetch: (url, o) => {
        calls.fetch.push({ url: url, options: o, body: JSON.parse(o.payload) });
        const next = statuses.length ? statuses.shift() : 200;
        if (next === 'throw') throw new Error('network down');
        return { getResponseCode: () => next };
      }
    },
    ScriptApp: {
      getProjectTriggers: () => [],
      newTrigger: () => ({ timeBased: () => ({ everyMinutes: () => ({ create: () => calls.log.push('trigger created') }) }) }),
      deleteTrigger: () => {}
    }
  };
  vm.createContext(ctx);
  vm.runInContext(SOURCE, ctx);
  return { ctx: ctx, store: store, calls: calls };
}

let passed = 0;
function t(name, fn) {
  try { fn(); passed += 1; console.log('ok   ' + name); }
  catch (e) { console.log('FAIL ' + name + '\n     ' + (e.stack || e)); process.exitCode = 1; }
}

// ---------- parsing ----------

t('alert subjects are recognised, everything else is not', () => {
  const s = sandbox([]).ctx;
  assert.strictEqual(s.isAlert('New job alert: Clean up spreadsheets'), true);
  assert.strictEqual(s.isAlert('Fwd: New job alert: x'), true);
  assert.strictEqual(s.isAlert('Payment received'), false);
  assert.strictEqual(s.isAlert('Your password was changed'), false);
  assert.strictEqual(s.isAlert(''), false);
});

t('extractJobs finds ids in plain and tracking links, with titles', () => {
  const jobs = sandbox([]).ctx.extractJobs(ALERT_HTML);
  assert.deepStrictEqual(Array.from(jobs, (j) => j.id), ['~01demoaaaaaaaaaa1', '~02demobbbbbbbbbb02']);
  assert.strictEqual(jobs[0].title, 'Clean up three spreadsheets');
  assert.strictEqual(jobs[1].title, 'Weekly report builder');
  assert.strictEqual(jobs[0].url, 'https://www.upwork.com/jobs/Clean-up-spreadsheets_~01demoaaaaaaaaaa1');
  assert.strictEqual(jobs[1].url, 'https://www.upwork.com/jobs/~02demobbbbbbbbbb02', 'the link is read out of a tracking redirect');
});

// ---------- job links: only allowed hosts get through ----------

const PREFIX = 'https://www.upwork.com/jobs/';

/** Runs extractJobs on one email link and returns the single job's url. */
function linkFor(href, id, ctxHook) {
  const s = sandbox([]).ctx;
  if (ctxHook) ctxHook(s);
  const jobs = s.extractJobs('<a href="' + href + '">Some job</a>');
  assert.strictEqual(jobs.length, 1, 'the job must never be dropped');
  assert.strictEqual(jobs[0].id, id);
  return jobs[0].url;
}

t('an allowed host keeps the email link (query string removed)', () => {
  assert.strictEqual(linkFor('https://www.upwork.com/jobs/Some-job_~01linkaaaaaaaaaa1/?referrer=email&x=1', '~01linkaaaaaaaaaa1'),
    'https://www.upwork.com/jobs/Some-job_~01linkaaaaaaaaaa1');
  assert.strictEqual(linkFor('https://upwork.com/jobs/~01linkaaaaaaaaaa1', '~01linkaaaaaaaaaa1'), 'https://upwork.com/jobs/~01linkaaaaaaaaaa1');
  assert.strictEqual(linkFor('https://WWW.UPWORK.COM/jobs/~01linkaaaaaaaaaa1', '~01linkaaaaaaaaaa1'),
    'https://WWW.UPWORK.COM/jobs/~01linkaaaaaaaaaa1', 'the host check ignores case');
});

t('a foreign host is replaced by the link built from the id, and the job is kept', () => {
  assert.strictEqual(linkFor('https://jobs.evil.example/jobs/~01linkbbbbbbbbbb2', '~01linkbbbbbbbbbb2'), PREFIX + '~01linkbbbbbbbbbb2');
});

t('lookalike hosts are refused', () => {
  const id = '~01linkcccccccccc3';
  for (const bad of [
    'https://upwork.com.evil.example/jobs/' + id,
    'https://www.upwork.com.evil.example/jobs/' + id,
    'https://evilupwork.com/jobs/' + id,
    'https://www-upwork.com/jobs/' + id,
    'https://www.upwork.com./jobs/' + id,
    'https://evil.example/www.upwork.com/jobs/' + id,
    'https://evil.example/https://www.upwork.com/jobs/' + id
  ]) {
    assert.strictEqual(linkFor(bad, id), PREFIX + id, bad);
  }
});

t('user-info, port, backslash and non-https tricks are refused', () => {
  const id = '~01linkdddddddddd4';
  for (const bad of [
    'https://www.upwork.com@evil.example/jobs/' + id,
    'https://www.upwork.com:8443/jobs/' + id,
    'https://evil.example\\@www.upwork.com/jobs/' + id,
    'http://www.upwork.com/jobs/' + id,
    '//www.upwork.com/jobs/' + id
  ]) {
    assert.strictEqual(linkFor(bad, id), PREFIX + id, bad);
  }
});

t('a tracking redirect that hides a foreign host is refused, plain or encoded', () => {
  const id = '~02linkeeeeeeeeee5';
  const hidden = 'https://evil.example/jobs/' + id;
  for (const bad of [
    'https://track.example.com/click?u=' + hidden,
    'https://track.example.com/click?u=' + encodeURIComponent(hidden),
    'https://track.example.com/click?u=' + encodeURIComponent(encodeURIComponent(hidden)),
    'https://www.upwork.com/redirect?to=' + encodeURIComponent(hidden)
  ]) {
    assert.strictEqual(linkFor(bad, id), PREFIX + id, bad);
  }
});

t('a tracking redirect to an allowed host yields the clean allowed link, never the tracker', () => {
  const id = '~02linkffffffffff6';
  const url = linkFor('https://track.example.com/click?u=' + encodeURIComponent('https://www.upwork.com/jobs/' + id), id);
  assert.strictEqual(url, 'https://www.upwork.com/jobs/' + id);
});

t('the allowed hosts and the fallback prefix come from CONFIG', () => {
  const id = '~01linkgggggggggg7';
  const hook = (s) => { s.CONFIG.LINK_HOSTS = ['jobs.example.org']; s.CONFIG.JOB_URL_PREFIX = 'https://jobs.example.org/j/'; };
  assert.strictEqual(linkFor('https://jobs.example.org/jobs/' + id, id, hook), 'https://jobs.example.org/jobs/' + id);
  assert.strictEqual(linkFor('https://www.upwork.com/jobs/' + id, id, hook), 'https://jobs.example.org/j/' + id);
  const defaults = sandbox([]).ctx.CONFIG;
  assert.deepStrictEqual(Array.from(defaults.LINK_HOSTS), ['www.upwork.com', 'upwork.com']);
  assert.strictEqual(defaults.JOB_URL_PREFIX, 'https://www.upwork.com/jobs/');
});

t('in a whole run every posted link is on an allowed host, and no job is lost', () => {
  const html = [
    '<a href="https://www.upwork.com/jobs/Real_~01runaaaaaaaaaaa1">Good one</a><p>Fixed price: $100</p>',
    '<a href="https://evil.example/jobs/~01runbbbbbbbbbbb2">Fake link</a><p>Fixed price: $100</p>',
    '<a href="https://track.example.com/c?u=' + encodeURIComponent('https://evil.example/x/~01runccccccccccc3') + '">Hidden</a><p>Fixed price: $100</p>'
  ].join('\n');
  const box = sandbox([emailMessage('m1', 'New job alert: mix', html)]);
  box.ctx.tick();
  const jobs = box.calls.fetch[0].body.jobs;
  assert.strictEqual(jobs.length, 3);
  for (const j of jobs) assert.ok(/^https:\/\/(www\.)?upwork\.com\/jobs\//.test(j.url), j.url);
  assert.strictEqual(jobs[1].url, PREFIX + '~01runbbbbbbbbbbb2');
  assert.strictEqual(jobs[2].url, PREFIX + '~01runccccccccccc3');
});

t('extractJobs reads payment and budget from the snippet, and strips links', () => {
  const jobs = sandbox([]).ctx.extractJobs(ALERT_HTML);
  assert.strictEqual(jobs[0].payment, 'fixed');
  assert.strictEqual(jobs[0].budget, 1250);
  assert.strictEqual(jobs[1].payment, 'hourly');
  assert.strictEqual(jobs[1].budget, 35);
  assert.ok(!/https?:/.test(jobs[1].description), jobs[1].description);
  assert.ok(jobs[0].description.length <= 600);
});

t('an email with no job link gives no jobs', () => {
  assert.strictEqual(sandbox([]).ctx.extractJobs('<p>Hello, <a href="https://example.com">site</a></p>').length, 0);
});

t('parseBudget and parsePayment return null when the text says nothing', () => {
  const s = sandbox([]).ctx;
  assert.strictEqual(s.parseBudget('no money here', null), null);
  assert.strictEqual(s.parsePayment('nothing useful'), null);
});

// ---------- the whole run against a fake Gmail ----------

t('a run posts new jobs once, with the key in a header and never in the body', () => {
  const box = sandbox([
    emailMessage('m1', 'New job alert: Clean up three spreadsheets', ALERT_HTML),
    emailMessage('m2', 'Weekly newsletter', '<a href="https://www.upwork.com/jobs/~01democcccccccccc3">Not an alert</a>')
  ]);
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 1);
  const f = box.calls.fetch[0];
  assert.strictEqual(f.url, HOOK);
  assert.strictEqual(f.options.headers['X-Api-Key'], KEY);
  assert.ok(!JSON.stringify(f.body).includes(KEY));
  assert.deepStrictEqual(Array.from(f.body.jobs, (j) => j.id), ['~01demoaaaaaaaaaa1', '~02demobbbbbbbbbb02']);
  assert.ok(!box.calls.gmail.includes('get:full:m2'), 'the body of a non-alert email must not be opened');
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 1, 'a second run must not post the same jobs again');
});

t('a refused post marks nothing, and the next run tries again', () => {
  const box = sandbox([emailMessage('m1', 'New job alert: x', ALERT_HTML)], { statuses: [500, 200] });
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 1);
  assert.ok(!Object.keys(box.store).some((k) => k.indexOf('j_') === 0 || k === 'm_m1'));
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 2);
  assert.ok('m_m1' in box.store);
});

t('a network error is handled like a refusal', () => {
  const box = sandbox([emailMessage('m1', 'New job alert: x', ALERT_HTML)], { statuses: ['throw', 200] });
  box.ctx.tick();
  assert.ok(!('m_m1' in box.store));
  box.ctx.tick();
  assert.ok('m_m1' in box.store);
});

t('missing Script properties post nothing and mark nothing', () => {
  const box = sandbox([emailMessage('m1', 'New job alert: x', ALERT_HTML)], { props: {} });
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 0);
  assert.ok(!('m_m1' in box.store));
});

t('many jobs go out in groups of 10; a failed group is retried, finished groups are not', () => {
  let html = '';
  for (let i = 0; i < 25; i++) {
    const id = '~01demo' + String(i).padStart(10, 'x') + 'zz';
    html += '<a href="https://www.upwork.com/jobs/' + id + '">Job number ' + i + '</a><p>Fixed price: $100</p>';
  }
  const box = sandbox([emailMessage('m1', 'New job alert: many', html)], { statuses: [200, 500, 200, 200] });
  box.ctx.tick();
  assert.deepStrictEqual(box.calls.fetch.map((c) => c.body.jobs.length), [10, 10], 'second group is refused, the run stops');
  assert.ok(!('m_m1' in box.store), 'the email stays open until every job in it is posted');
  box.ctx.tick();
  assert.deepStrictEqual(box.calls.fetch.map((c) => c.body.jobs.length), [10, 10, 10, 5]);
  assert.ok('m_m1' in box.store);
  const accepted = [box.calls.fetch[0], box.calls.fetch[2], box.calls.fetch[3]].reduce((all, c) => all.concat(c.body.jobs.map((j) => j.id)), []);
  assert.strictEqual(accepted.length, 25);
  assert.strictEqual(new Set(accepted).size, 25, 'no job is accepted twice');
});

t('old entries are pruned', () => {
  const old = String(Date.now() - 10 * 24 * 3600 * 1000);
  const box = sandbox([], { props: { WEBHOOK_URL: HOOK, API_KEY: KEY, m_old: old, j_old: old, keepme: 'x' } });
  box.ctx.tick();
  assert.ok(!('m_old' in box.store) && !('j_old' in box.store));
  assert.strictEqual(box.store.keepme, 'x');
});

t('setup marks existing alerts as seen, so there is no backlog', () => {
  const box = sandbox([emailMessage('m1', 'New job alert: x', ALERT_HTML)]);
  box.ctx.setup();
  assert.ok('m_m1' in box.store && 'j_~01demoaaaaaaaaaa1' in box.store);
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 0);
});

t('the key is never written to the log', () => {
  const box = sandbox([emailMessage('m1', 'New job alert: x', ALERT_HTML)], { statuses: [500] });
  box.ctx.tick();
  box.ctx.setup();
  assert.ok(box.calls.log.length > 0);
  assert.ok(!box.calls.log.join('\n').includes(KEY));
});

// ---------- read-only guarantees ----------

t('only Users.Messages.list and Users.Messages.get are ever called on Gmail', () => {
  const used = new Set(SOURCE.match(/\bGmail(?:\.[A-Za-z]+)+/g) || []);
  assert.deepStrictEqual(Array.from(used).sort(), ['Gmail.Users.Messages.get', 'Gmail.Users.Messages.list']);
  assert.ok(!/GmailApp/.test(SOURCE), 'the simple GmailApp service needs wider access');
});

t('the manifest asks for the read-only Gmail scope and nothing wider', () => {
  const gmail = MANIFEST.oauthScopes.filter((s) => /gmail|mail\.google\.com/.test(s));
  assert.deepStrictEqual(gmail, ['https://www.googleapis.com/auth/gmail.readonly']);
  assert.ok(!('webapp' in MANIFEST), 'the watcher is not a web app, nothing can call it');
});

t('during a run, Gmail only receives list and get calls', () => {
  const box = sandbox([emailMessage('m1', 'New job alert: x', ALERT_HTML)]);
  box.ctx.tick();
  assert.ok(box.calls.gmail.every((c) => c === 'list' || c.indexOf('get:') === 0), box.calls.gmail.join(','));
});

// ---------- budget labels ----------

t('a budget is read only after a budget label, never from a client line', () => {
  const s = sandbox([]).ctx;
  assert.strictEqual(s.parseBudget('Fixed-price · Intermediate · Est. budget: $300 · Posted 4 minutes ago · Payment verified · $5K+ spent', 'fixed'), 300);
  assert.strictEqual(s.parseBudget('Hourly: $25.00-$40.00 · Expert · Payment verified · $50K+ spent', 'hourly'), 40);
  assert.strictEqual(s.parseBudget('Hourly rate $15 to $30', 'hourly'), 30);
  assert.strictEqual(s.parseBudget('Fixed price: $1,250. Need data cleanup.', 'fixed'), 1250);
  assert.strictEqual(s.parseBudget('Budget: $1.5K', 'fixed'), 1500);
  assert.strictEqual(s.parseBudget('Budget – $80', 'fixed'), 80);
  assert.strictEqual(s.parseBudget('Payment verified · $50K+ spent · Germany', 'hourly'), null, 'a client line is not a budget');
  assert.strictEqual(s.parseBudget('Fixed-price · Entry level · $200 spent by the client', 'fixed'), null);
  assert.strictEqual(s.parseBudget('The budget is flexible, we paid $40 last time', 'fixed'), null);
});

t('in a whole alert the client line never becomes the budget', () => {
  const html = '<a href="https://www.upwork.com/jobs/~02labelaaaaaaaaaaaaa1">KPI report</a><p>Hourly: $25.00-$40.00</p><p>Payment verified · $50K+ spent</p>' +
               '<a href="https://www.upwork.com/jobs/~02labelaaaaaaaaaaaaa2">Sheet fix</a><p>Payment verified · $5K+ spent</p><p>Fixed-price · Est. budget: $40</p>';
  const jobs = sandbox([]).ctx.extractJobs(html);
  assert.deepStrictEqual(Array.from(jobs, (j) => [j.payment, j.budget]), [['hourly', 40], ['fixed', 40]]);
});

// ---------- ids anywhere in the email ----------

function plainMessage(id, subject, text) {
  return { id: id, internalDate: String(Date.now()),
           payload: { headers: [{ name: 'Subject', value: subject }], mimeType: 'text/plain', body: { data: B64(text) } } };
}

t('a plain-text alert is read: id from the bare link, title from the subject, budget from its label', () => {
  const text = 'Excel dashboard for sales data\nFixed-price - Est. budget: $400\nhttps://www.upwork.com/jobs/~02plainaaaaaaaaaaaa1?ref=email\n\nUnsubscribe: https://example.com/u';
  const box = sandbox([plainMessage('p1', 'New job alert: Excel dashboard for sales data', text)]);
  box.ctx.tick();
  const jobs = box.calls.fetch[0].body.jobs;
  assert.strictEqual(jobs.length, 1);
  assert.deepStrictEqual([jobs[0].id, jobs[0].url, jobs[0].title, jobs[0].payment, jobs[0].budget],
    ['~02plainaaaaaaaaaaaa1', 'https://www.upwork.com/jobs/~02plainaaaaaaaaaaaa1', 'Excel dashboard for sales data', 'fixed', 400]);
  assert.ok('m_p1' in box.store);
});

t('several jobs in a plain-text alert each get their own block', () => {
  const text = 'Sheet cleanup\nFixed-price - Budget: $300\nhttps://www.upwork.com/jobs/~02plainbbbbbbbbbbbb1\n\nWeekly report\nHourly: $20-$30\nhttps://evil.example/jobs/~02plainbbbbbbbbbbbb2\n';
  const jobs = sandbox([]).ctx.extractJobs(text, 'New job alert: Sheet cleanup');
  assert.deepStrictEqual(Array.from(jobs, (j) => [j.title, j.payment, j.budget, j.url]), [
    ['Sheet cleanup', 'fixed', 300, 'https://www.upwork.com/jobs/~02plainbbbbbbbbbbbb1'],
    ['Weekly report', 'hourly', 30, 'https://www.upwork.com/jobs/~02plainbbbbbbbbbbbb2']]);
});

t('an id outside any link, or hidden as %7E in a tracker, is found with a clean link', () => {
  const s = sandbox([]).ctx;
  const bare = s.extractJobs('<p>Job: https://www.upwork.com/jobs/~02bareaaaaaaaaaaaaa1</p><p>Budget: $90</p>', 'New job alert: Bare link job');
  assert.deepStrictEqual([bare[0].id, bare[0].url, bare[0].title], ['~02bareaaaaaaaaaaaaa1', 'https://www.upwork.com/jobs/~02bareaaaaaaaaaaaaa1', 'Bare link job']);
  const hidden = s.extractJobs('<a href="https://click.example.net/c?t=https%253A%252F%252Fwww.upwork.com%252Fjobs%252F%257E02hiddenaaaaaaaaaaa1">Hidden job</a>');
  assert.deepStrictEqual([hidden[0].id, hidden[0].url, hidden[0].title], ['~02hiddenaaaaaaaaaaa1', 'https://www.upwork.com/jobs/~02hiddenaaaaaaaaaaa1', 'Hidden job']);
});

// ---------- an alert with no job is never marked read in silence ----------

const NO_JOB = '<a href="https://click.example.net/ls/click?upn=u001.b3BhcXVlLXRva2Vu">Inventory spreadsheet audit</a><p>Budget: $250</p>';

t('an alert with no readable job is posted as a note, and marked read only once the webhook took it', () => {
  const box = sandbox([emailMessage('n1', 'New job alert: Inventory spreadsheet audit', NO_JOB)], { statuses: [500, 200] });
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 1);
  assert.deepStrictEqual(box.calls.fetch[0].body, { jobs: [], unreadable: [{ id: 'n1', subject: 'New job alert: Inventory spreadsheet audit' }] });
  assert.ok(!('m_n1' in box.store), 'refused, so not marked');
  assert.ok(box.calls.log.some((l) => /No job could be read from the alert "New job alert: Inventory spreadsheet audit"/.test(l)), box.calls.log.join('|'));
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 2);
  assert.ok('m_n1' in box.store);
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 2, 'posted once');
});

t('jobs and unreadable alerts go in separate posts; an alert whose jobs were all seen is not a note', () => {
  const box = sandbox([emailMessage('a1', 'New job alert: x', ALERT_HTML), emailMessage('n1', 'New job alert: y', NO_JOB)]);
  box.ctx.tick();
  assert.deepStrictEqual(box.calls.fetch.map((c) => [c.body.jobs.length, (c.body.unreadable || []).length]), [[2, 0], [0, 1]]);
  const again = sandbox([emailMessage('a2', 'New job alert: same jobs again', ALERT_HTML)], { props: Object.assign({}, box.store) });
  again.ctx.tick();
  assert.strictEqual(again.calls.fetch.length, 0);
  assert.ok('m_a2' in again.store);
});

t('a long subject is cut to 200 characters in the note', () => {
  const box = sandbox([emailMessage('n2', 'New job alert: ' + 'x'.repeat(400), NO_JOB)]);
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch[0].body.unreadable[0].subject.length, 200);
});

// ---------- https only ----------

t('a WEBHOOK_URL that is not https posts nothing, marks nothing and says why', () => {
  const box = sandbox([emailMessage('m1', 'New job alert: x', ALERT_HTML)], { props: { WEBHOOK_URL: 'http://hooks.example.com/jobs', API_KEY: KEY } });
  box.ctx.tick();
  assert.strictEqual(box.calls.fetch.length, 0);
  assert.ok(!('m_m1' in box.store));
  assert.ok(box.calls.log.some((l) => /must start with https/.test(l)));
  box.ctx.setup();
  assert.ok(/WEBHOOK_URL \(it must start with https:\/\/\)/.test(box.calls.log[box.calls.log.length - 1]));
});

// ---------- HTML codes ----------

t('every HTML code in a title is decoded, once', () => {
  const s = sandbox([]).ctx;
  assert.strictEqual(s.decodeEntities('Don&rsquo;t &amp; won&#39;t &#8212; caf&eacute; &middot; &#x1F600; &AMP;'), 'Don’t & won\'t — café · \u{1F600} &');
  assert.strictEqual(s.decodeEntities('&amp;lt;b&amp;gt;'), '&lt;b&gt;', 'decoded once, so it can never become a tag');
  assert.strictEqual(s.decodeEntities('&nosuchcode; &#0; &#xD800;'), '&nosuchcode; &#0; &#xD800;');
  assert.strictEqual(s.LATIN1_NAMES.length, 96);
  const jobs = s.extractJobs('<a href="https://www.upwork.com/jobs/~02codeaaaaaaaaaaaaa1">Client&rsquo;s sheet &ndash; caf&eacute; menu</a>');
  assert.strictEqual(jobs[0].title, 'Client’s sheet – café menu');
});

t('diagnose logs every job with its payment type and budget', () => {
  const box = sandbox([emailMessage('m1', 'New job alert: x', ALERT_HTML), emailMessage('n1', 'New job alert: y', NO_JOB)]);
  box.ctx.diagnose();
  const log = box.calls.log.join('\n');
  assert.ok(/New job alert: x: 2 job\(s\)/.test(log), log);
  assert.ok(/~01demoaaaaaaaaaa1  fixed  \$1250  Clean up three spreadsheets/.test(log), log);
  assert.ok(/New job alert: y: 0 job\(s\), so a note would go to Telegram/.test(log), log);
  assert.strictEqual(box.calls.fetch.length, 0);
});

console.log(passed + ' watcher tests passed' + (process.exitCode ? ', some FAILED' : ''));
