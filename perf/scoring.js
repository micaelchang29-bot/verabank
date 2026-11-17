// Prueba de carga de risk-scoring (docs/diseno.md §1.2): POST /internal/v1/scores directo, sin transfer-service.
//
//   make perf-scoring [DURATION=30m] [PEAK=1]
//
// Escenarios (constant-arrival-rate, llegadas independientes de la latencia):
//   warmup  50 solicitudes/s durante 60 s, sin umbrales (JIT, pool, cachés).
//   steady  50 solicitudes/s durante DURATION (5m por defecto; 30m para el NFR de §1.2).
//   peak    solo con PEAK=1: 100 solicitudes/s durante 5 min, a continuación de steady.
// Umbrales (solo sobre steady y peak): p(95) < 150 ms, p(99) < 300 ms y menos de 1 % de errores.
//
// Las solicitudes se generan aquí: UUID propio, 5.000 orígenes y 2.000 destinos (con prefijo perf-, para
// distinguirlos en risk.scoring_log) y occurred_at que avanza una hora de negocio cada 50 solicitudes.
import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8083';
const DURATION = __ENV.DURATION || '5m';
const PEAK = __ENV.PEAK === '1';
const SOURCES = 5000;
const DESTINATIONS = 2000;
const EPOCH_MS = Date.parse('2026-01-01T00:00:00Z');
const HOUR_MS = 3600 * 1000;
const PER_HOUR = 50;
const TYPES = ['TRANSFER', 'CASH_OUT', 'PAYMENT', 'DEBIT'];

function seconds(text) {
  const m = /^(\d+)(s|m|h)$/.exec(text);
  if (!m) throw new Error(`DURATION inválida: ${text} (ejemplos: 90s, 5m, 30m)`);
  return Number(m[1]) * { s: 1, m: 60, h: 3600 }[m[2]];
}

const WARMUP_S = 60;
const STEADY_S = seconds(DURATION);
const PEAK_S = 300;

function arrival(name, rate, startS, durationS) {
  return {
    executor: 'constant-arrival-rate',
    exec: 'score',
    rate,
    timeUnit: '1s',
    duration: `${durationS}s`,
    startTime: `${startS}s`,
    preAllocatedVUs: Math.ceil(rate / 2),
    maxVUs: rate * 4,
    gracefulStop: '10s',
    tags: { phase: name },
  };
}

const scenarios = {
  warmup: arrival('warmup', 50, 0, WARMUP_S),
  steady: arrival('steady', 50, WARMUP_S, STEADY_S),
};
const thresholds = {
  'http_req_duration{scenario:steady}': ['p(95)<150', 'p(99)<300'],
  'http_req_failed{scenario:steady}': ['rate<0.01'],
};
if (PEAK) {
  scenarios.peak = arrival('peak', 100, WARMUP_S + STEADY_S, PEAK_S);
  thresholds['http_req_duration{scenario:peak}'] = ['p(95)<150', 'p(99)<300'];
  thresholds['http_req_failed{scenario:peak}'] = ['rate<0.01'];
}

export const options = { scenarios, thresholds, summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'] };

function hex(n) {
  let out = '';
  for (let i = 0; i < n; i++) out += Math.floor(Math.random() * 16).toString(16);
  return out;
}

// UUID v4 propio (sin importaciones remotas).
function uuid() {
  const variant = '89ab'[Math.floor(Math.random() * 4)];
  return `${hex(8)}-${hex(4)}-4${hex(3)}-${variant}${hex(3)}-${hex(12)}`;
}

function amount() {
  // Log-normal acotada: la mayoría de montos son pequeños, con cola larga.
  const u = Math.random() + Math.random() + Math.random() - 1.5;
  return Math.min(500000, Math.max(1, Math.exp(5.5 + 1.6 * u))).toFixed(2);
}

export function score() {
  const n = exec.scenario.iterationInTest;
  const payload = {
    transfer_id: uuid(),
    source_ref: `perf-src-${String(Math.floor(Math.random() * SOURCES)).padStart(4, '0')}`,
    dest_ref: `perf-dst-${String(Math.floor(Math.random() * DESTINATIONS)).padStart(4, '0')}`,
    amount: amount(),
    currency: 'PEN',
    transfer_type: TYPES[Math.floor(Math.random() * TYPES.length)],
    dest_country: 'PE',
    occurred_at: new Date(EPOCH_MS + Math.floor(n / PER_HOUR) * HOUR_MS).toISOString().replace('.000Z', 'Z'),
  };
  const res = http.post(`${BASE_URL}/internal/v1/scores`, JSON.stringify(payload), {
    headers: { 'Content-Type': 'application/json', 'X-Correlation-Id': uuid() },
  });
  if (res.status !== 200) {
    // Una línea por fallo, con el instante y el segundo de la prueba, para ver si se concentran o son periódicos.
    console.error(`FALLO ${JSON.stringify({ at: new Date().toISOString(), t_s: Math.round(exec.instance.currentTestRunDuration / 100) / 10, scenario: exec.scenario.name, status: res.status, ms: Math.round(res.timings.duration), body: String(res.body).slice(0, 120) })}`);
  }
  check(res, {
    'status 200': (r) => r.status === 200,
    'decisión válida': (r) => r.status === 200 && ['APPROVE', 'REVIEW', 'BLOCK'].includes(r.json('decision')),
  });
}
