// Dedicated, disposable benchmark bank. Never connects to a public cluster.
import { createServer } from 'node:http';
import { randomBytes } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { LocalRuntime, json } from '../../apps/payout-demo/bridge.mjs';

const config = resolve(process.argv[2]);
const runtime = await LocalRuntime.start();
// Fixture funding is declared separately from transaction fees and rent.
runtime.surf.fundSol(runtime.owner, 1_000_000_000_000);
runtime.surf.fundSol(runtime.executor, 100_000_000_000);
const token = randomBytes(32).toString('hex');
let chain = Promise.resolve();
const server = createServer((req, res) => {
  if (req.method !== 'POST' || req.headers.authorization !== `Bearer ${token}`) {
    res.writeHead(403).end(); return;
  }
  let body = '';
  req.on('data', chunk => { body += chunk; if (body.length > 65536) req.destroy(); });
  req.on('end', () => {
    chain = chain.then(async () => {
      const calls = runtime.calls, methods = { ...runtime.methodCounts }, started = performance.now();
      try {
        const command = JSON.parse(body);
        if (command.action === 'stop_comparison') {
          if (command.instance_id !== runtime.surf.instanceId) throw Error('bank_identity_mismatch');
          res.writeHead(200, { 'Content-Type': 'application/json' }).end(json({ stopped: true }));
          server.close(() => { runtime.close(); process.exit(0); }); return;
        }
        const result = await runtime.dispatch(command);
        res.writeHead(200, { 'Content-Type': 'application/json' }).end(json(result));
      } catch (error) {
        res.writeHead(400, { 'Content-Type': 'application/json' }).end(json({
          error: String(error.message).slice(0, 500),
          ...runtime.measurementsSince(calls, methods, started),
        }));
      }
    });
  });
});
server.listen(0, '127.0.0.1', () => {
  mkdirSync(dirname(config), { recursive: true });
  writeFileSync(config, json({
    url: `http://127.0.0.1:${server.address().port}`, token, pid: process.pid,
    instance_id: runtime.surf.instanceId,
  }), { mode: 0o600 });
  console.log('Disposable comparison bank ready.');
});
const stop = () => server.close(() => { runtime.close(); process.exit(0); });
process.on('SIGTERM', stop);
process.on('SIGINT', stop);
