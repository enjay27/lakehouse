// The stress scenario (ADR-053 §3): one k6 run at one constant rate, started by
// gates/stress/driver.py in the k6 pod, once per step of a ramp and once for the hold.
//
//   k6 run -e PROFILE=mixed -e RATE=200 -e SECONDS=30 -e NAME=mixed-02 \
//     -e POLARIS=http://polaris:8181 -e REALM=POLARIS -e CATALOG=gate_stress_catalog scenario.js
//
// It reads /stress/config.json (gates/stress/config.json) and /stress/tables.json (the seed's
// result, gates/stress/seed.py). Root's credentials come from the pod's environment
// (ROOT_CLIENT_ID, ROOT_CLIENT_SECRET, the Secret polaris-root): setup() rotates gate_stress's
// credentials with them, and every request after that is gate_stress's.
//
// - `main`, an open-model arrival rate: each iteration picks a request kind by the profile's
//   shares. Loads and HEADs go to any table; commits go to the tables outside the hot set.
// - `hot`, when the profile commits: writers_per_table writers per hot table commit in a loop,
//   so commits collide and the 409 path runs.
// - Each VU is one client and keeps its own ETags, as Iceberg Java 1.11 does, for etag_ttl_s.
//   A conditional load sends If-None-Match; a full load never does. Loads read no body
//   (responseType 'none'), so k6 never parses the 1 MB answers.
// - A commit loads the table (conditionally when this client knows its snapshot), pulls the
//   current snapshot id and the last sequence number out of a 200 with indexOf, then POSTs
//   add-snapshot and set-snapshot-ref on main with assert-ref-snapshot-id. Ids stay strings:
//   Iceberg's are 63-bit and JavaScript numbers hold 53. On a 409 it reloads and retries, up
//   to commit_retries times. Every POST logs `C sent <table> <snapshot id>` to the console
//   before it goes, and `C <outcome> <table> <snapshot id>` after, for the driver's commit
//   accounting: ok (200), conflict (409), unknown (no answer or 5xx: the commit may have
//   happened), failed (any other answer). A `sent` with no outcome after it was cut short when
//   the run ended, and is unknown too.
//
// handleSummary writes /tmp/<NAME>.json: requests by kind and status class, latency by kind,
// commit outcomes by kind, and the iterations k6 dropped. No credential is in it.
//
// Nothing tests this file in the static gate (no JavaScript runner there): `k6 inspect` and a
// run check it.

import http from 'k6/http';
import exec from 'k6/execution';
import { sleep } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const config = JSON.parse(open(__ENV.STRESS_CONFIG || '/stress/config.json'));
const tables = JSON.parse(open(__ENV.STRESS_TABLES || '/stress/tables.json')).tables;

const KINDS = ['token', 'config', 'list_namespaces', 'list_tables', 'head_table',
  'conditional_load', 'full_load', 'commit', 'commit_hot'];
const CLASSES = ['2xx', '304', '409', '4xx', '5xx', 'error'];
const OUTCOMES = ['ok', 'conflict', 'unknown', 'failed'];

const PROFILE = __ENV.PROFILE;
const RATE = Number(__ENV.RATE);
const SECONDS = Number(__ENV.SECONDS);
const NAME = __ENV.NAME;
const BASE = __ENV.POLARIS;
const REALM = __ENV.REALM;
const CATALOG = __ENV.CATALOG;

const profile = config.profiles[PROFILE];
if (!profile || !(RATE > 0) || !(SECONDS > 0) || !NAME) {
  throw new Error(`unknown profile ${PROFILE}, or RATE, SECONDS or NAME missing`);
}
const mix = [];
let total = 0;
for (const [kind, share] of Object.entries(profile.shares)) {
  if (!KINDS.includes(kind) || kind === 'commit_hot') throw new Error(`unknown kind ${kind}`);
  total += share;
  mix.push([total, kind]);
}
const hotTables = tables.filter((t) => t.hot);
const ordinary = tables.filter((t) => !t.hot);
const namespaces = [...new Set(tables.map((t) => t.name.split('.')[0]))];
const commits = 'commit' in profile.shares;

const requests = {};
const latency = {};
const outcomes = {};
for (const kind of KINDS) {
  requests[kind] = {};
  for (const c of CLASSES) requests[kind][c] = new Counter(`req_${kind}_${c}`);
  latency[kind] = new Trend(`lat_${kind}`, true);
}
for (const kind of ['commit', 'commit_hot']) {
  outcomes[kind] = {};
  for (const o of OUTCOMES) outcomes[kind][o] = new Counter(`op_${kind}_${o}`);
}

const scenarios = {
  main: {
    executor: 'constant-arrival-rate', exec: 'main', rate: RATE, timeUnit: '1s',
    duration: `${SECONDS}s`, preAllocatedVUs: config.k6.pre_vus, maxVUs: config.k6.max_vus,
    gracefulStop: '10s',
  },
};
if (commits && hotTables.length > 0) {
  scenarios.hot = {
    executor: 'constant-vus', exec: 'hot', vus: hotTables.length * config.hot.writers_per_table,
    duration: `${SECONDS}s`, gracefulStop: '10s',
  };
}

export const options = {
  scenarios,
  setupTimeout: '120s',
  summaryTrendStats: ['med', 'p(95)', 'p(99)', 'max', 'count'],
};

function statusClass(status) {
  if (status === 0) return 'error';
  if (status === 304) return '304';
  if (status === 409) return '409';
  if (status >= 200 && status < 300) return '2xx';
  if (status >= 500) return '5xx';
  return '4xx';
}

function send(kind, method, url, body, headers, responseType) {
  const res = http.request(method, url, body, {
    headers, responseType, timeout: config.k6.timeout, tags: { name: kind },
  });
  latency[kind].add(res.timings.duration);
  requests[kind][statusClass(res.status)].add(1);
  return res;
}

function tableUrl(name) {
  const [namespace, table] = name.split('.');
  return `${BASE}/api/catalog/v1/${CATALOG}/namespaces/${namespace}/tables/${table}`;
}

function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

// The digits after `"key":` in a JSON text, as a string, or null.
function field(text, key) {
  const at = text.indexOf(`"${key}"`);
  if (at < 0) return null;
  const match = /^\s*:\s*(-?\d+)/.exec(text.substring(at + key.length + 2, at + key.length + 40));
  return match ? match[1] : null;
}

function newSnapshotId() {
  return String(Math.floor(Math.random() * 8e15) + 1e15);
}

function token(clientId, clientSecret) {
  const res = http.post(`${BASE}/api/catalog/v1/oauth/tokens`, {
    grant_type: 'client_credentials', client_id: clientId, client_secret: clientSecret,
    scope: 'PRINCIPAL_ROLE:ALL',
  }, { headers: { 'Polaris-Realm': REALM } });
  if (res.status !== 200) throw new Error(`token: HTTP ${res.status}`);
  return res.json('access_token');
}

export function setup() {
  const root = token(__ENV.ROOT_CLIENT_ID, __ENV.ROOT_CLIENT_SECRET);
  const res = http.post(`${BASE}/api/management/v1/principals/gate_stress/rotate`, null, {
    headers: { Authorization: `Bearer ${root}`, 'Polaris-Realm': REALM },
  });
  if (res.status !== 200) throw new Error(`rotating gate_stress: HTTP ${res.status}`);
  const credentials = res.json('credentials');
  return {
    clientId: credentials.clientId, clientSecret: credentials.clientSecret,
    token: token(credentials.clientId, credentials.clientSecret),
  };
}

// This VU's ETags: table name -> {etag, at, snapshot, sequence}.
const cache = {};

function fresh(name) {
  const entry = cache[name];
  return entry && Date.now() - entry.at < config.etag_ttl_s * 1000 ? entry : null;
}

function load(kind, headers, table, conditional) {
  const entry = conditional ? fresh(table.name) : null;
  const h = entry ? Object.assign({ 'If-None-Match': entry.etag }, headers) : headers;
  const res = send(kind, 'GET', tableUrl(table.name), null, h, 'none');
  if (conditional && res.status === 200) {
    cache[table.name] = { etag: res.headers.Etag, at: Date.now() };
  }
}

function commit(kind, headers, table) {
  const url = tableUrl(table.name);
  for (let attempt = 0; attempt <= config.commit_retries; attempt++) {
    const entry = fresh(table.name);
    const known = entry && entry.snapshot ? entry : null;
    const h = known ? Object.assign({ 'If-None-Match': known.etag }, headers) : headers;
    const got = send(kind, 'GET', url, null, h, 'text');
    let snapshot;
    let sequence;
    if (got.status === 304 && known) {
      ({ snapshot, sequence } = known);
    } else if (got.status === 200) {
      snapshot = field(got.body, 'current-snapshot-id');
      sequence = Number(field(got.body, 'last-sequence-number'));
      cache[table.name] = { etag: got.headers.Etag, at: Date.now(), snapshot, sequence };
    }
    if (!snapshot) {
      outcomes[kind].failed.add(1);
      return;
    }
    const id = newSnapshotId();
    const body = `{"requirements":[{"type":"assert-ref-snapshot-id","ref":"main",`
      + `"snapshot-id":${snapshot}}],"updates":[{"action":"add-snapshot","snapshot":`
      + `{"snapshot-id":${id},"parent-snapshot-id":${snapshot},`
      + `"sequence-number":${sequence + 1},"timestamp-ms":${Date.now()},`
      + `"manifest-list":${JSON.stringify(table.manifest_list)},`
      + `"summary":{"operation":"append"},"schema-id":0}},`
      + `{"action":"set-snapshot-ref","ref-name":"main","type":"branch","snapshot-id":${id}}]}`;
    console.log(`C sent ${table.name} ${id}`);
    const res = send(kind, 'POST', url, body,
      Object.assign({ 'Content-Type': 'application/json' }, headers), 'none');
    delete cache[table.name];
    let outcome = 'failed';
    if (res.status === 200) outcome = 'ok';
    else if (res.status === 409) outcome = 'conflict';
    else if (res.status === 0 || res.status >= 500) outcome = 'unknown';
    console.log(`C ${outcome} ${table.name} ${id}`);
    if (outcome !== 'conflict') {
      outcomes[kind][outcome].add(1);
      return;
    }
  }
  outcomes[kind].conflict.add(1);
}

export function main(data) {
  const headers = { Authorization: `Bearer ${data.token}`, 'Polaris-Realm': REALM };
  const r = Math.random() * total;
  const kind = (mix.find(([upTo]) => r < upTo) || mix[mix.length - 1])[1];
  if (kind === 'conditional_load') load(kind, headers, pick(tables), true);
  else if (kind === 'full_load') load(kind, headers, pick(tables), false);
  else if (kind === 'commit') commit(kind, headers, pick(ordinary));
  else if (kind === 'head_table') send(kind, 'HEAD', tableUrl(pick(tables).name), null, headers, 'none');
  else if (kind === 'list_tables') {
    send(kind, 'GET', `${BASE}/api/catalog/v1/${CATALOG}/namespaces/${pick(namespaces)}/tables`,
      null, headers, 'none');
  } else if (kind === 'list_namespaces') {
    send(kind, 'GET', `${BASE}/api/catalog/v1/${CATALOG}/namespaces`, null, headers, 'none');
  } else if (kind === 'config') {
    send(kind, 'GET', `${BASE}/api/catalog/v1/config?warehouse=${CATALOG}`, null, headers, 'none');
  } else if (kind === 'token') {
    send(kind, 'POST', `${BASE}/api/catalog/v1/oauth/tokens`,
      { grant_type: 'client_credentials', client_id: data.clientId,
        client_secret: data.clientSecret, scope: 'PRINCIPAL_ROLE:ALL' },
      { 'Polaris-Realm': REALM }, 'none');
  }
}

export function hot(data) {
  const headers = { Authorization: `Bearer ${data.token}`, 'Polaris-Realm': REALM };
  commit('commit_hot', headers, hotTables[exec.vu.idInTest % hotTables.length]);
  sleep(config.hot.pause_ms / 1000);
}

export function handleSummary(data) {
  const m = data.metrics;
  const count = (name) => (m[name] ? m[name].values.count : 0);
  const out = {
    profile: PROFILE, rate: RATE, seconds: SECONDS, offered: RATE * SECONDS,
    dropped: count('dropped_iterations'), requests: {}, latency_ms: {}, commits: {},
  };
  for (const kind of KINDS) {
    const byClass = {};
    for (const c of CLASSES) byClass[c] = count(`req_${kind}_${c}`);
    if (Object.values(byClass).some((n) => n > 0)) out.requests[kind] = byClass;
    const lat = m[`lat_${kind}`];
    if (lat) {
      out.latency_ms[kind] = {
        p50: lat.values.med, p95: lat.values['p(95)'], p99: lat.values['p(99)'],
        max: lat.values.max, count: lat.values.count,
      };
    }
  }
  for (const kind of Object.keys(outcomes)) {
    const byOutcome = {};
    for (const o of OUTCOMES) byOutcome[o] = count(`op_${kind}_${o}`);
    if (Object.values(byOutcome).some((n) => n > 0)) out.commits[kind] = byOutcome;
  }
  return { [`/tmp/${NAME}.json`]: JSON.stringify(out) };
}
