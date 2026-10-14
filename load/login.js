// Logins per second: bcrypt with cost 12 is slow on purpose, so this is CPU-bound.
// docker run --rm --add-host=host.docker.internal:host-gateway -v "$PWD/load:/s" -e RATE=10 grafana/k6:1.3.0 run /s/login.js
import http from 'k6/http';
import { check } from 'k6';

const API = __ENV.API_URL || 'http://host.docker.internal:8000';
export const options = {
  scenarios: { login: { executor: 'constant-arrival-rate', rate: +(__ENV.RATE || 10), timeUnit: '1s', duration: '20s',
                        preAllocatedVUs: 20, maxVUs: 200 } },
  summaryTrendStats: ['avg', 'p(50)', 'p(95)', 'max'],
};

export default function () {
  const res = http.post(`${API}/api/v1/auth/login`, { username: 'viewer', password: __ENV.VIEWER_PASSWORD || 'Viewer@12345' });
  check(res, { 'status 200': (x) => x.status === 200 });
}
