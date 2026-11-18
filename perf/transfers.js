// Prueba de carga de POST /api/v1/transfers (T-17, docs/diseno.md §1.2 y §6). Sin importaciones remotas.
//
//   make load-transfers [DURATION=5m|30m] [PEAK=1]
//
// Escenarios (constant-arrival-rate: las llegadas no dependen de la latencia):
//   warmup  RATE solicitudes/s durante 60 s, sin umbrales (JIT, pools, cachés).
//   steady  RATE (50) solicitudes/s durante DURATION (5m por defecto; 30m para el NFR).
//   peak    solo con PEAK=1: +RATE solicitudes/s durante 5 min desde el minuto 25 de steady (100 TPS en total).
// Mezcla (D10): 70 % EXTERNAL y 30 % INTERNAL; el 2 % de las creadas se reintenta con la misma clave y el mismo cuerpo
// (debe dar 200 con Idempotent-Replayed: true). Canal propio `loadtest` (API_KEY). Cuentas de make load-seed:
// orígenes md5('verabank-load:src:<i>')::uuid y destinos internos md5('verabank-load:dst:<j>')::uuid.
// Umbrales (solo steady y peak): http_req_duration p(95) < 400 ms y < 1 % de respuestas fuera de 200, 201 y 422.
// handleSummary escribe /out/transfers-summary.json (data/perf/ en el host).
import http from 'k6/http';
import crypto from 'k6/crypto';
import { Counter, Rate } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://transfer-service:8081';
const API_KEY = __ENV.API_KEY || '';
const DURATION = __ENV.DURATION || '5m';
const PEAK = __ENV.PEAK === '1';
const RATE = parseInt(__ENV.RATE || '50', 10);
const ACCOUNTS = parseInt(__ENV.ACCOUNTS || '5000', 10);
const DESTINATIONS = parseInt(__ENV.DESTINATIONS || '2000', 10);
const RETRY_RATE = parseFloat(__ENV.RETRY_RATE || '0.02');
const EXTERNAL_RATE = parseFloat(__ENV.EXTERNAL_RATE || '0.7');
const VUS = parseInt(__ENV.VUS || '200', 10);

const created = new Counter('res_201');
const replayed = new Counter('res_200_replayed');
const replayWrong = new Counter('res_200_not_replayed');
const declined = new Counter('res_422_transfer_declined');
const insufficient = new Counter('res_422_insufficient_funds');
const otherUnprocessable = new Counter('res_422_other');
const other = new Counter('res_other');
const retries = new Counter('retries_sent');
const unexpected = new Rate('unexpected_status');

const scenarios = {
  warmup: {
    executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: '60s',
    preAllocatedVUs: VUS, maxVUs: VUS * 2, tags: { phase: 'warmup' },
  },
  steady: {
    executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: DURATION, startTime: '60s',
    preAllocatedVUs: VUS, maxVUs: VUS * 2, tags: { phase: 'load' },
  },
};
if (PEAK) {
  scenarios.peak = {
    executor: 'constant-arrival-rate', rate: RATE, timeUnit: '1s', duration: '5m', startTime: '1560s', // 60 s + 25 min
    preAllocatedVUs: VUS, maxVUs: VUS * 2, tags: { phase: 'load' },
  };
}

export const options = {
  scenarios,
  thresholds: {
    'http_req_duration{phase:load}': ['p(95)<400'],
    'unexpected_status{phase:load}': ['rate<0.01'],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

function hexToUuid(hex) {
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
}

// md5(texto)::uuid de PostgreSQL: los 32 dígitos hexadecimales del md5 con guiones.
function seedUuid(label) {
  return hexToUuid(crypto.md5(label, 'hex'));
}

// UUID v4 aleatorio generado aquí (sin dependencias).
function uuidv4() {
  const b = new Uint8Array(crypto.randomBytes(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  let hex = '';
  for (let i = 0; i < 16; i += 1) hex += b[i].toString(16).padStart(2, '0');
  return hexToUuid(hex);
}

function pick(n) {
  return Math.floor(Math.random() * n);
}

function body() {
  const source = seedUuid(`verabank-load:src:${pick(ACCOUNTS)}`);
  const amount = (10 + Math.random() * 490).toFixed(2);   // 10–500: bajo el umbral AML y el límite del fallback
  const base = {
    source_account_id: source,
    transfer_type: Math.random() < 0.5 ? 'TRANSFER' : 'PAYMENT',
    amount,
    currency: 'PEN',
    dest_country: 'PE',
    occurred_at: new Date().toISOString(),
  };
  if (Math.random() < EXTERNAL_RATE) {
    return { ...base, rail: 'EXTERNAL', dest_external_ref: `LT-EXT-${String(pick(DESTINATIONS)).padStart(4, '0')}` };
  }
  return { ...base, rail: 'INTERNAL', dest_account_id: seedUuid(`verabank-load:dst:${pick(DESTINATIONS)}`) };
}

function post(key, payload, tags) {
  return http.post(`${BASE_URL}/api/v1/transfers`, payload, {
    headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY, 'Idempotency-Key': key },
    tags,
    timeout: '30s',
  });
}

function classify(res, isRetry) {
  const ok = res.status === 200 || res.status === 201 || res.status === 422;
  unexpected.add(!ok);
  if (res.status === 201) {
    created.add(1);
  } else if (res.status === 200) {
    if (isRetry && res.headers['Idempotent-Replayed'] === 'true') replayed.add(1); else replayWrong.add(1);
  } else if (res.status === 422) {
    let type = '';
    try { type = res.json('type') || ''; } catch (e) { type = ''; }
    if (type.endsWith('transfer-declined')) declined.add(1);
    else if (type.endsWith('insufficient-funds')) insufficient.add(1);
    else otherUnprocessable.add(1);
  } else {
    other.add(1);
  }
}

export default function () {
  const key = uuidv4();
  const payload = JSON.stringify(body());
  const res = post(key, payload, { kind: 'create' });
  classify(res, false);
  if (res.status === 201 && Math.random() < RETRY_RATE) {
    retries.add(1);
    classify(post(key, payload, { kind: 'retry' }), true);
  }
}

function count(data, name) {
  const m = data.metrics[name];
  return m ? m.values.count : 0;
}

export function handleSummary(data) {
  const d = data.metrics['http_req_duration{phase:load}'];
  const summary = {
    duration: DURATION, peak: PEAK, rate: RATE,
    counts: {
      '201': count(data, 'res_201'),
      '200_replayed': count(data, 'res_200_replayed'),
      '200_not_replayed': count(data, 'res_200_not_replayed'),
      '422_transfer_declined': count(data, 'res_422_transfer_declined'),
      '422_insufficient_funds': count(data, 'res_422_insufficient_funds'),
      '422_other': count(data, 'res_422_other'),
      other: count(data, 'res_other'),
      retries_sent: count(data, 'retries_sent'),
      requests: count(data, 'http_reqs'),
    },
    load_latency_ms: d ? d.values : null,
    unexpected_rate_load: data.metrics['unexpected_status{phase:load}']
      ? data.metrics['unexpected_status{phase:load}'].values.rate : null,
    thresholds_ok: Object.values(data.metrics).every((m) => !m.thresholds
      || Object.values(m.thresholds).every((t) => t.ok)),
  };
  return {
    '/out/transfers-summary.json': JSON.stringify(summary, null, 2),
    stdout: `\nT-17: ${JSON.stringify(summary.counts)}\np95 de carga: ${d ? d.values['p(95)'].toFixed(1) : '-'} ms; `
      + `fuera de 200/201/422: ${(100 * (summary.unexpected_rate_load || 0)).toFixed(3)} %; umbrales `
      + `${summary.thresholds_ok ? 'OK' : 'NO cumplidos'}\n`,
  };
}
