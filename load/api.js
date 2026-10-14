// REST mix at a constant request rate: record lists, analytics and latest readings.
// docker run --rm --add-host=host.docker.internal:host-gateway -v "$PWD/load:/s" -e RATE=200 grafana/k6:1.3.0 run /s/api.js
import http from 'k6/http';
import { check } from 'k6';

const BASE = `${__ENV.API_URL || 'http://host.docker.internal:8000'}/api/v1`;
export const options = {
  scenarios: { reads: { executor: 'constant-arrival-rate', rate: +(__ENV.RATE || 100), timeUnit: '1s',
                        duration: __ENV.DURATION || '30s', preAllocatedVUs: 50, maxVUs: 300 } },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  const r = http.post(`${BASE}/auth/login`, { username: 'viewer', password: __ENV.VIEWER_PASSWORD || 'Viewer@12345' });
  return { headers: { Authorization: `Bearer ${r.json('access_token')}` } };
}

const paths = [
  ['records', '/records?page=1&page_size=20', 0.4],
  ['summary', '/analytics/summary?category=cpu_temp', 0.15],
  ['trend', '/analytics/trend?interval=day', 0.1],
  ['latest', '/realtime/latest?limit=120', 0.25],
  ['categories', '/records/categories', 0.1],
];

export default function (auth) {
  let pick = Math.random();
  for (const [name, path, weight] of paths) {
    pick -= weight;
    if (pick <= 0) {
      const res = http.get(`${BASE}${path}`, Object.assign({ tags: { ep: name } }, auth));
      check(res, { 'status 200': (x) => x.status === 200 });
      return;
    }
  }
}
