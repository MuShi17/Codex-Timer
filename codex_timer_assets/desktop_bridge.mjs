// Codex desktop's local app-tools protocol: uint32 LE length + JSON-RPC.
// Input arrives over stdin; never place prompts or pipe addresses in argv.
import net from 'node:net';

const MAX_FRAME = 8 * 1024 * 1024;
let input = '';
for await (const chunk of process.stdin) {
  input += chunk;
  if (Buffer.byteLength(input) > MAX_FRAME) throw new Error('Request too large');
}
const request = JSON.parse(input);
let sent = false, finished = false, buffered = Buffer.alloc(0);
const socket = net.createConnection(request.pipe);
const timeout = setTimeout(() => finish({error: 'Desktop tool request timed out'}), request.timeoutMs);
function finish(result) {
  if (finished) return;
  finished = true;
  clearTimeout(timeout);
  socket.destroy();
  process.stdout.write(JSON.stringify({sent, ...result}));
}
socket.on('error', error => finish({error: error.message}));
socket.on('end', () => finish({error: 'Desktop tool channel disconnected'}));
socket.on('connect', () => {
  const payload = Buffer.from(JSON.stringify(request.rpc));
  if (payload.length > MAX_FRAME) return finish({error: 'Request too large'});
  const frame = Buffer.alloc(payload.length + 4);
  frame.writeUInt32LE(payload.length);
  payload.copy(frame, 4);
  sent = true; // A subsequent failure cannot prove that a mutation was rejected.
  socket.write(frame);
});
socket.on('data', chunk => {
  buffered = Buffer.concat([buffered, chunk]);
  if (buffered.length < 4) return;
  const length = buffered.readUInt32LE();
  if (length > MAX_FRAME) return finish({error: 'Response too large'});
  if (buffered.length < length + 4) return;
  try {
    const response = JSON.parse(buffered.subarray(4, length + 4).toString('utf8'));
    if (response.id !== request.rpc.id) throw new Error('Response id mismatch');
    finish({response});
  } catch (error) { finish({error: error.message}); }
});
