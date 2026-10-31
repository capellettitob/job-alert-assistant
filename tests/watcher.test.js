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

console.log(passed + ' watcher tests passed' + (process.exitCode ? ', some FAILED' : ''));
