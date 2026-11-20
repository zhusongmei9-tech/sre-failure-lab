// One iteration sends exactly one product request. The Python driver prepares the cache
// before starting k6 so warm-up and readiness probes cannot enter the measurements.
import http from 'k6/http';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';

const scenario = __ENV.CAP_SCENARIO;
const started = new Counter('cap_started');
const completed = new Counter('cap_completed');
const valid = new Counter('cap_valid');
const http2xx = new Counter('cap_http_2xx');
const http429 = new Counter('cap_http_429');
const http5xx = new Counter('cap_http_5xx');
const timeout = new Counter('cap_timeouts');
const transport = new Counter('cap_transport_errors');
const latency = new Trend('cap_client_ms', true);
const cache = {};
for (const outcome of ['HIT', 'MISS', 'BYPASS', 'UNKNOWN']) {
  cache[outcome] = new Counter('cap_cache_' + outcome.toLowerCase());
}
const coldMiss = new Counter('cap_initial_misses');

export const options = {
  scenarios: {
    products: {
      executor: 'constant-arrival-rate', rate: Number(__ENV.CAP_RATE), timeUnit: '1s',
      duration: __ENV.CAP_SECONDS + 's', preAllocatedVUs: Number(__ENV.CAP_VUS),
      gracefulStop: '5s',
    },
  },
  summaryTrendStats: ['p(95)', 'p(99)'],
  maxRedirects: 0,
};

export default function () {
  const index = exec.scenario.iterationInTest;
  const id = scenario === 'hot' ? 1 : 1 + (index % 3);
  started.add(1);
  const before = Date.now();
  const res = http.get(__ENV.CAP_BASE + '/products/' + id, {timeout: '2s'});
  latency.add(Date.now() - before);
  completed.add(1);
  const successStatus = res.status >= 200 && res.status < 300;
  if (successStatus) http2xx.add(1);
  if (res.status === 429) http429.add(1);
  if (res.status >= 500 && res.status < 600) http5xx.add(1);
  if (res.status === 0) transport.add(1);
  if (res.error_code === 1050 || res.error_code === 1211) timeout.add(1);

  let product = null;
  if (successStatus) {
    try { product = res.json(); } catch (_) { /* malformed successful response */ }
  }
  const correct = product && product.id === id && typeof product.name === 'string' &&
    product.name.length > 0 && Number.isInteger(product.price_cents) && product.price_cents >= 0 &&
    Number.isInteger(product.stock) && product.stock >= 0 && product.currency === 'CNY';
  if (correct) valid.add(1);
  const header = res.headers['X-Cache'];
  const outcome = ['HIT', 'MISS', 'BYPASS'].includes(header) ? header : 'UNKNOWN';
  cache[outcome].add(1);
  // Only the first three scheduled IDs (1, 2, 3) qualify as cold-start evidence.
  if (scenario === 'cold-start' && index < 3 && correct && outcome === 'MISS') coldMiss.add(1);
}

export function handleSummary(data) {
  return { [__ENV.CAP_SUMMARY]: JSON.stringify(data.metrics) };
}
