// Isolated app-tools fixture. It never connects to the user's Codex app.
import net from 'node:net';
import fs from 'node:fs';
const [pipe, control, log] = process.argv.slice(2);
const server = net.createServer(socket => {
  let buffer = Buffer.alloc(0);
  socket.on('error', () => {});
  socket.on('data', chunk => {
    buffer = Buffer.concat([buffer, chunk]);
    if (buffer.length < 4 || buffer.length < buffer.readUInt32LE() + 4) return;
    const request = JSON.parse(buffer.subarray(4, buffer.readUInt32LE() + 4));
    const state = JSON.parse(fs.readFileSync(control, 'utf8'));
    const args = request.params.arguments;
    let result;
    if (request.method === 'tools/list') {
      result = {tools: ['read_thread', 'send_message_to_thread'].map(name => ({namespace:'codex_app', name}))};
    } else if (request.params.tool === 'read_thread') {
      result = {thread: {id:args.threadId, hostId:state.host ?? 'local', kind:'codex',
                         status:{type:state.status}}, turns: [{id:state.turnId, status:state.status==='active'?'inProgress':'completed'}]};
    } else if (request.params.tool === 'send_message_to_thread') {
      fs.appendFileSync(log, JSON.stringify({prompt:args.prompt, threadId:args.threadId,
                        callId:request.params.callId, turnId:state.turnId, before:state.status})+'\n');
      if (state.disconnect) return socket.destroy();
      if (state.timeout) return;
      if (state.rpcError) return respond({error:{code:-32000,message:'Codex app tool request failed'}});
      if (state.toolError) return respond({result:{success:false,contentItems:[{type:'inputText',text:'Unknown failure'}]}});
      if (state.malformed) return respond({result:null});
      if (state.noResult) return respond({});
      result = {threadId:args.threadId, turnId:state.status==='active'?state.turnId:'new-turn'};
      if ('ack' in state) result = state.ack;
    }
    if (request.method !== 'tools/list') result={success:true, contentItems:[{type:'inputText',text:JSON.stringify(result)}]};
    respond({result});
    function respond(value) {
      const data = Buffer.from(JSON.stringify({jsonrpc:'2.0', id:request.id, ...value}));
      const frame = Buffer.alloc(data.length+4); frame.writeUInt32LE(data.length); data.copy(frame,4);
      // Deliberately fragmented frames test the real transport reader.
      socket.write(frame.subarray(0,2));
      setTimeout(() => socket.end(frame.subarray(2)), 5);
    }
  });
});
server.listen(pipe, () => console.log('ready'));
