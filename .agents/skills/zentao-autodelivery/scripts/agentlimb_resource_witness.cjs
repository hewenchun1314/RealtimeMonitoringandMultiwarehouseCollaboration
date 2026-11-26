const { createHash } = require('node:crypto');
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

function enableWitness(settings) {
  const resources = new Set(settings.resources.map(resource => {
    const url = new URL(resource.url);
    return JSON.stringify([Number(url.port || (url.protocol === 'https:' ? 443 : 80)), url.pathname + url.search]);
  }));
  for (const server of [Server, SecureServer]) {
    const emit = server.prototype.emit;
    server.prototype.emit = function (event, request, response) {
      if (event === 'request' && request.method === 'GET' &&
          resources.has(JSON.stringify([request.socket.localPort, request.url]))) {
        delete request.headers['if-none-match'];
        delete request.headers['if-modified-since'];
        witnessResponse(response, settings.origin, settings.gatewayHash);
      }
      return emit.apply(this, arguments);
    };
  }
}

if (process.env.ZENTAO_E2E_WITNESS) enableWitness(JSON.parse(process.env.ZENTAO_E2E_WITNESS));

module.exports = { witnessResponse };
