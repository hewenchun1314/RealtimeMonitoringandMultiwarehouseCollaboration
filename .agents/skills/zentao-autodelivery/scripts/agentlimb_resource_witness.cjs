const { createHash } = require('node:crypto');
const { realpathSync } = require('node:fs');
const { Server } = require('node:http');
const { Server: SecureServer } = require('node:https');
const { brotliDecompressSync, gunzipSync, inflateSync } = require('node:zlib');

const limit = 16 * 1024 * 1024;
const decoders = { br: brotliDecompressSync, gzip: gunzipSync, deflate: inflateSync };

// Stamp the bytes of the actual response, before Chrome or Wujie consumes them.
// Only the listed complete asset responses are buffered. Large files and streams
// retain their ordinary response behavior and produce no version evidence.
function witnessResponse(response, origin, gatewayHash) {
  const { write, end, writeHead, flushHeaders } = response;
  const chunks = [];
  let size = 0;

  function restore() {
    Object.assign(response, { write, end, writeHead, flushHeaders });
  }

  function flush() {
    restore();
    for (const chunk of chunks) write.call(response, chunk);
    chunks.length = 0;
  }

  function collect(chunk, encoding) {
    if (chunk == null) return true;
    const bytes = Buffer.from(chunk, typeof encoding === 'string' ? encoding : undefined);
    size += bytes.length;
    if (size > limit) return false;
    chunks.push(bytes);
    return true;
  }

  response.writeHead = function (status, message, headers) {
    response.statusCode = status;
    if (typeof message === 'string') response.statusMessage = message;
    else headers = message;
    if (Array.isArray(headers)) {
      for (let index = 0; index < headers.length; index += 2) response.appendHeader(headers[index], headers[index + 1]);
    } else if (headers) {
      for (const [name, value] of Object.entries(headers)) response.setHeader(name, value);
    }
    return response;
  };

  response.flushHeaders = function () {
    flush();
    return flushHeaders.call(response);
  };

  response.write = function (chunk, encoding, callback) {
    if (!collect(chunk, encoding)) {
      flush();
      return write.call(response, chunk, encoding, callback);
    }
    const done = typeof encoding === 'function' ? encoding : callback;
    if (typeof done === 'function') process.nextTick(done);
    return true;
  };

  response.end = function (chunk, encoding, callback) {
    const done = [chunk, encoding, callback].find(value => typeof value === 'function');
    if (typeof chunk !== 'function' && !collect(chunk, encoding)) {
      flush();
      return end.call(response, chunk, encoding, callback);
    }
    const body = Buffer.concat(chunks);
    if (response.statusCode === 200 && body.length && !response.headersSent && !response.getHeader('content-range')) {
      try {
        const encoding = String(response.getHeader('content-encoding') || 'identity').toLowerCase();
        const decoded = encoding === 'identity' ? body : decoders[encoding](body, { maxOutputLength: limit });
        const digest = createHash('sha256').update(decoded).digest('hex');
        response.appendHeader('Server-Timing', `zentao-resource;desc="${digest}"`);
        if (gatewayHash) response.appendHeader('Server-Timing', `zentao-gateway;desc="${gatewayHash}"`);
        response.setHeader('Timing-Allow-Origin', origin);
        response.setHeader('Cache-Control', 'no-store');
      } catch {
        // An unsupported or invalid encoding cannot supply a reliable digest.
      }
    }
    restore();
    return end.call(response, body, done);
  };
}

// For a new run the isolated checkout already gives us the expected versions.
// Mark each successful script or style response from the app's own server.
function stampLaunchResponse(response, origin, identity, gatewayHash) {
  const { writeHead } = response;
  response.writeHead = function (status, message, headers) {
    if (typeof message !== 'string') headers = message;
    if (status !== 200) return writeHead.apply(response, arguments);
    const inline = Array.isArray(headers)
      ? headers.reduce((pairs, value, index) => {
          if (index % 2 === 0) pairs.push([value, headers[index + 1]]);
          return pairs;
        }, [])
      : Object.entries(headers || {});
    const pairs = name => inline.filter(([key]) => key.toLowerCase() === name).map(([, value]) => value);
    const contentType = pairs('content-type').at(-1) ?? response.getHeader('content-type') ?? '';
    if (!/^(?:text\/css|(?:text|application)\/(?:x-)?(?:java|ecma)script)\b/i.test(contentType) ||
        pairs('content-range').length || response.hasHeader('content-range')) {
      return writeHead.apply(response, arguments);
    }

    const timing = [...(pairs('server-timing').length ? pairs('server-timing') : [response.getHeader('server-timing')]),
      'zentao-launch;desc="' + identity + '"'];
    if (gatewayHash) timing.push('zentao-gateway;desc="' + gatewayHash + '"');
    const overrides = { 'server-timing': timing.filter(Boolean).flat().join(', '),
      'timing-allow-origin': origin, 'cache-control': 'no-store' };
    const changed = inline.filter(([name]) => !Object.hasOwn(overrides, name.toLowerCase())).flat();
    for (const [name, value] of Object.entries(overrides)) changed.push(name, value);
    return typeof message === 'string' ? writeHead.call(response, status, message, changed)
      : writeHead.call(response, status, changed);
  };
}

function enableWitness(settings) {
  const launch = settings.launch;
  const app = launch?.roots[realpathSync.native(process.cwd())];
  if (launch && !app) return;
  const resources = new Set(settings.resources.map(resource => {
    const url = new URL(resource.url);
    return JSON.stringify([Number(url.port || (url.protocol === 'https:' ? 443 : 80)), url.pathname + url.search]);
  }));
  for (const server of [Server, SecureServer]) {
    const emit = server.prototype.emit;
    server.prototype.emit = function (event, request, response) {
      if (event === 'request' && request.method === 'GET') {
        const port = request.socket.localPort;
        if (launch ? launch.ports.includes(port) : resources.has(JSON.stringify([port, request.url]))) {
          delete request.headers['if-none-match'];
          delete request.headers['if-modified-since'];
          if (launch) stampLaunchResponse(response, settings.origin, launch.token + '.' + app, settings.gatewayHash);
          else witnessResponse(response, settings.origin, settings.gatewayHash);
        }
      }
      return emit.apply(this, arguments);
    };
  }
}

if (process.env.ZENTAO_E2E_WITNESS) enableWitness(JSON.parse(process.env.ZENTAO_E2E_WITNESS));

module.exports = { witnessResponse, stampLaunchResponse };
