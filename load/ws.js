// Many WebSocket clients on the live stream; counts the messages they receive.
// docker run --rm --add-host=host.docker.internal:host-gateway -v "$PWD/load:/s" -e CLIENTS=20 grafana/k6:1.3.0 run /s/ws.js
import http from 'k6/http';
import ws from 'k6/ws';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const API = __ENV.API_URL || 'http://host.docker.internal:8000';
const received = new Counter('ws_messages');
export const options = { vus: +(__ENV.CLIENTS || 20), duration: '25s' };

export function setup() {
  const r = http.post(`${API}/api/v1/auth/login`, { username: 'viewer', password: __ENV.VIEWER_PASSWORD || 'Viewer@12345' });
  return r.json('access_token');
}

export default function (token) {
  const res = ws.connect(`${API.replace('http', 'ws')}/api/v1/ws/realtime?token=${token}`, {}, (socket) => {
    socket.on('message', () => received.add(1));
    socket.setTimeout(() => socket.close(), 20000);  // one message per second: about 20 per client
  });
  check(res, { 'status 101': (r) => r && r.status === 101 });
}
