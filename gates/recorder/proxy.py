"""The gate's recorder (ADR-034): sits between Polaris and a service it calls, forwards every
byte unchanged in both directions, and writes one JSON line per exchange to stdout.

    python3 proxy.py [--http NAME=PORT:HOST:UPSTREAM_PORT]... [--pg NAME=PORT:HOST:UPSTREAM_PORT]...
                     [--tls-cert FILE --tls-key FILE]

Forwarding is transparent: the bytes a client sends are the bytes the upstream receives, every
header (`Host`, `Authorization`, `x-amz-security-token`) and every PostgreSQL message included,
so S3 signatures and SCRAM logins work as without the recorder. The streams are parsed on the
side: a stream the recorder cannot parse loses its records (one `parse_error` record says so),
never its bytes. The one exception is PostgreSQL TLS, which a recorder cannot read through:
when a client asks for TLS, the recorder answers with the upstream's answer, and on yes it
accepts TLS with --tls-cert/--tls-key (generated in the gate job) and opens TLS to PostgreSQL.

Records never hold a credential or a stored value (Actions logs are public, ADR-002):

- HTTP: method, path, query, status, duration, headers (Authorization, Proxy-Authorization,
  x-amz-security-token, Cookie, Set-Cookie and the X-Amz-Credential, X-Amz-Signature and
  X-Amz-Security-Token query parameters as `<redacted>`), and each body: JSON up to 64 KiB in
  full, any other body as its length and sha256.
- PostgreSQL: one `connect` record per login (user, database, application name, TLS, and the
  SQLSTATE when it fails), then one `statement` record per command: its SQL text, the number of
  bind parameters, the result's column names and row count, and the command tag, or the
  error's severity, SQLSTATE and message. Never an authentication message, a bind parameter
  value, a row value or an error's detail field.

Python standard library only; runs from the pinned Python image with this file mounted from a
ConfigMap (ADR-034).
"""

import argparse
import collections
import datetime
import hashlib
import json
import select
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.parse

BODY_LIMIT = 64 * 1024
REDACTED = "<redacted>"
REDACTED_HEADERS = {"authorization", "proxy-authorization", "x-amz-security-token", "cookie",
                    "set-cookie"}
REDACTED_QUERY = {"x-amz-credential", "x-amz-signature", "x-amz-security-token"}
SSL_REQUEST, GSSENC_REQUEST, CANCEL_REQUEST = 80877103, 80877104, 80877102

_emit_lock = threading.Lock()


def emit(record, out=None):
    line = json.dumps(record, separators=(",", ":"), sort_keys=True)
    out = out or sys.stdout
    with _emit_lock:
        out.write(line + "\n")
        out.flush()


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def redact_headers(headers):
    out = {}
    for name, value in headers:
        name = name.lower()
        value = REDACTED if name in REDACTED_HEADERS else value
        out[name] = f"{out[name]}, {value}" if name in out else value
    return out


def split_target(target):
    """(path, query dict with credentials redacted) of a request target."""
    parts = urllib.parse.urlsplit(target)
    query = {}
    for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        query[key] = REDACTED if key.lower() in REDACTED_QUERY else value
    return parts.path, query


class Body:
    """A body's length and sha256, and its first BODY_LIMIT + 1 bytes."""

    def __init__(self):
        self.length, self.hash, self.head = 0, hashlib.sha256(), bytearray()

    def add(self, data):
        self.length += len(data)
        self.hash.update(data)
        if len(self.head) <= BODY_LIMIT:
            self.head += data[:BODY_LIMIT + 1 - len(self.head)]

    def summary(self, content_type):
        if self.length and "json" in (content_type or "") and self.length <= BODY_LIMIT:
            try:
                return {"length": self.length, "json": json.loads(bytes(self.head))}
            except ValueError:
                pass
        return {"length": self.length, "sha256": self.hash.hexdigest()}


class HttpStream:
    """One direction of an HTTP/1.1 connection, parsed passively. Calls on_head(message) when a
    message's head is complete and on_done(message) when its body is; skips 1xx responses."""

    def __init__(self, is_response, on_head, on_done, method_of_next_response=None):
        self.is_response, self.on_head, self.on_done = is_response, on_head, on_done
        self.method_of_next_response = method_of_next_response
        self.buf, self.state, self.message, self.remaining = bytearray(), "head", None, 0

    def feed(self, data):
        self.buf += data
        while self._step():
            pass

    def eof(self):
        if self.state == "until_eof":
            self._finish()

    def _finish(self):
        message, self.message, self.state = self.message, None, "head"
        self.on_done(message)

    def _step(self):
        if self.state == "head":
            end = self.buf.find(b"\r\n\r\n")
            if end < 0:
                return False
            lines = bytes(self.buf[:end]).decode("iso-8859-1").split("\r\n")
            del self.buf[:end + 4]
            headers = [tuple(part.strip() for part in line.split(":", 1)) for line in lines[1:]
                       if ":" in line]
            message = {"start": lines[0], "headers": headers, "body": Body(),
                       "time": now(), "t0": time.monotonic()}
            get = {k.lower(): v for k, v in headers}
            start = lines[0].split(" ", 2)
            if (start[0] if self.is_response else start[-1])[:5] != "HTTP/" or len(start) < 2:
                raise ValueError(f"not an HTTP start line: {lines[0][:40]!r}")
            if self.is_response:
                status = int(start[1])
                message["status"] = status
                if 100 <= status < 200:
                    return True
                method = self.method_of_next_response() if self.method_of_next_response else ""
                no_body = method == "HEAD" or status in (204, 304)
            else:
                message["method"], message["target"] = start[:2]
                no_body = False
            self.message = message
            self.on_head(message)
            if no_body:
                self._finish()
            elif "chunked" in get.get("transfer-encoding", "").lower():
                self.state = "chunk_size"
            elif "content-length" in get:
                self.remaining = int(get["content-length"])
                self.state = "length"
                if not self.remaining:
                    self._finish()
            elif self.is_response:
                self.state = "until_eof"
            else:
                self._finish()
            return True
        if self.state in ("length", "chunk_data"):
            if not self.buf:
                return False
            take = bytes(self.buf[:self.remaining])
            del self.buf[:len(take)]
            self.message["body"].add(take)
            self.remaining -= len(take)
            if self.remaining:
                return False
            if self.state == "length":
                self._finish()
            else:
                self.state = "chunk_crlf"
            return True
        if self.state == "until_eof":
            self.message["body"].add(bytes(self.buf))
            self.buf.clear()
            return False
        end = self.buf.find(b"\r\n")
        if end < 0:
            return False
        line = bytes(self.buf[:end])
        del self.buf[:end + 2]
        if self.state == "chunk_size":
            self.remaining = int(line.split(b";", 1)[0].strip(), 16)
            self.state = "chunk_data" if self.remaining else "trailers"
        elif self.state == "chunk_crlf":
            self.state = "chunk_size"
        elif self.state == "trailers" and not line:
            self._finish()
        return True


class HttpRecorder:
    """Pairs a connection's requests with its responses; one record per exchange."""

    def __init__(self, listener, out=None):
        self.listener, self.out, self.pending = listener, out, collections.deque()
        self.requests = HttpStream(False, self.pending.append, lambda m: None)
        self.responses = HttpStream(True, lambda m: None, self._response,
                                    lambda: self.pending[0]["method"] if self.pending else "")

    def _response(self, response):
        request = self.pending.popleft() if self.pending else None
        record = {"proto": "http", "listener": self.listener, "status": response["status"],
                  "response_headers": redact_headers(response["headers"]),
                  "response_body": response["body"].summary(
                      dict(redact_headers(response["headers"])).get("content-type"))}
        if request:
            path, query = split_target(request["target"])
            headers = redact_headers(request["headers"])
            record.update({
                "time": request["time"], "method": request["method"], "path": path,
                "query": query, "request_headers": headers,
                "request_body": request["body"].summary(headers.get("content-type")),
                "duration_ms": round((time.monotonic() - request["t0"]) * 1000, 1)})
        emit(record, self.out)

    def client_bytes(self, data):
        self.requests.feed(data)

    def upstream_bytes(self, data):
        self.responses.feed(data)

    def closed(self):
        self.responses.eof()
        for request in self.pending:
            path, query = split_target(request["target"])
            emit({"proto": "http", "listener": self.listener, "time": request["time"],
                  "method": request["method"], "path": path, "query": query,
                  "request_headers": redact_headers(request["headers"]), "status": None,
                  "note": "connection closed before a response"}, self.out)


def _cstring(payload, at):
    end = payload.index(b"\0", at)
    return payload[at:end].decode("utf-8", "replace"), end + 1


class PgMessages:
    """Splits a PostgreSQL byte stream into (type, payload); the frontend's first message is the
    untyped startup message, given as type None."""

    def __init__(self, startup):
        self.buf, self.startup = bytearray(), startup

    def feed(self, data):
        self.buf += data
        while True:
            if self.startup:
                if len(self.buf) < 4:
                    return
                (length,) = struct.unpack("!I", self.buf[:4])
                if len(self.buf) < length:
                    return
                payload = bytes(self.buf[4:length])
                del self.buf[:length]
                self.startup = False
                yield None, payload
                continue
            if len(self.buf) < 5:
                return
            kind = chr(self.buf[0])
            (length,) = struct.unpack("!I", self.buf[1:5])
            if len(self.buf) < 1 + length:
                return
            payload = bytes(self.buf[5:1 + length])
            del self.buf[:1 + length]
            yield kind, payload


class PgRecorder:
    """Pairs a connection's statements with their results; see the module docstring."""

    def __init__(self, listener, tls, out=None):
        self.listener, self.tls, self.out = listener, tls, out
        self.frontend, self.backend = PgMessages(startup=True), PgMessages(startup=False)
        self.session, self.authenticated = {}, False
        self.statements, self.portals, self.queue = {}, {}, collections.deque()

    def _base(self, event):
        return {"proto": "pg", "listener": self.listener, "event": event, "time": now(),
                "user": self.session.get("user"), "database": self.session.get("database")}

    def client_bytes(self, data):
        for kind, payload in self.frontend.feed(data):
            self._frontend(kind, payload)

    def upstream_bytes(self, data):
        for kind, payload in self.backend.feed(data):
            self._backend(kind, payload)

    def closed(self):
        pass

    def _frontend(self, kind, payload):
        t0 = time.monotonic()
        if kind is None:
            (code,) = struct.unpack("!I", payload[:4])
            if code == CANCEL_REQUEST:
                emit(self._base("cancel"), self.out)
                return
            fields = payload[4:].split(b"\0")
            pairs = dict(zip(fields[0::2], fields[1::2]))
            self.session = {k.decode(): v.decode("utf-8", "replace") for k, v in pairs.items()
                            if k in (b"user", b"database", b"application_name")}
        elif kind == "Q":
            self.queue.append({"kind": "simple", "sql": payload[:-1].decode("utf-8", "replace"),
                               "params": 0, "t0": t0, "columns": [], "rows": 0})
        elif kind == "P":
            name, at = _cstring(payload, 0)
            sql, at = _cstring(payload, at)
            self.statements[name] = sql
        elif kind == "B":
            portal, at = _cstring(payload, 0)
            statement, at = _cstring(payload, at)
            (formats,) = struct.unpack("!H", payload[at:at + 2])
            at += 2 + 2 * formats
            (params,) = struct.unpack("!H", payload[at:at + 2])
            self.portals[portal] = (self.statements.get(statement), params)
        elif kind == "E":
            portal, _ = _cstring(payload, 0)
            sql, params = self.portals.get(portal, (None, 0))
            self.queue.append({"kind": "extended", "sql": sql, "params": params, "t0": t0,
                               "columns": [], "rows": 0})
        elif kind == "S":
            self.queue.append({"kind": "sync"})

    def _head(self):
        return self.queue[0] if self.queue and self.queue[0]["kind"] != "sync" else None

    def _statement(self, entry, **result):
        record = self._base("statement")
        record.update({"sql": entry["sql"] if entry else None,
                       "params": entry["params"] if entry else 0,
                       "columns": entry["columns"] if entry else [],
                       "rows": entry["rows"] if entry else 0, **result})
        if entry:
            record["duration_ms"] = round((time.monotonic() - entry["t0"]) * 1000, 1)
        emit(record, self.out)

    def _backend(self, kind, payload):
        if kind == "R":
            (code,) = struct.unpack("!I", payload[:4])
            if code == 0:
                self.authenticated = True
                emit({**self._base("connect"), "tls": self.tls, "result": "ok",
                      "application_name": self.session.get("application_name")}, self.out)
        elif kind == "E":
            error = {}
            at = 0
            while at < len(payload) and payload[at] != 0:
                code = chr(payload[at])
                value, at = _cstring(payload, at + 1)
                if code in "VCM":
                    error[{"V": "severity", "C": "sqlstate", "M": "message"}[code]] = value
            if not self.authenticated:
                emit({**self._base("connect"), "tls": self.tls, "result": "error",
                      "application_name": self.session.get("application_name"),
                      "error": error}, self.out)
                return
            self._statement(self._head(), error=error)
        elif kind == "T":
            entry = self._head()
            if entry is not None:
                (count,) = struct.unpack("!H", payload[:2])
                at, names = 2, []
                for _ in range(count):
                    name, at = _cstring(payload, at)
                    names.append(name)
                    at += 18
                entry["columns"] = names
        elif kind == "D":
            entry = self._head()
            if entry is not None:
                entry["rows"] += 1
        elif kind in ("C", "I", "s"):
            entry = self._head()
            tag = {"I": "EMPTY", "s": "SUSPENDED"}.get(kind) or payload[:-1].decode()
            self._statement(entry, tag=tag)
            if entry is not None:
                if entry["kind"] == "extended":
                    self.queue.popleft()
                else:
                    entry.update({"columns": [], "rows": 0})
        elif kind == "Z":
            if self.queue and self.queue[0]["kind"] == "simple":
                self.queue.popleft()
            else:
                while self.queue:
                    if self.queue.popleft()["kind"] == "sync":
                        break


def _recv(sock):
    try:
        return sock.recv(65536)
    except ssl.SSLWantReadError:
        return None
    except OSError:
        return b""


def relay(client, upstream, recorder):
    """Copies bytes both ways until both sides are done, feeding each to the recorder."""
    open_from = {client: True, upstream: True}
    peer = {client: upstream, upstream: client}
    feed = {client: recorder.client_bytes, upstream: recorder.upstream_bytes}
    broken = False
    while any(open_from.values()):
        socks = [s for s, is_open in open_from.items() if is_open]
        ready = [s for s in socks if isinstance(s, ssl.SSLSocket) and s.pending()]
        if not ready:
            ready, _, _ = select.select(socks, [], [])
        for sock in ready:
            data = _recv(sock)
            if data is None:
                continue
            if not data:
                open_from[sock] = False
                other = peer[sock]
                if isinstance(other, ssl.SSLSocket) or isinstance(sock, ssl.SSLSocket):
                    open_from[other] = False
                else:
                    try:
                        other.shutdown(socket.SHUT_WR)
                    except OSError:
                        open_from[other] = False
                continue
            try:
                peer[sock].sendall(data)
            except OSError:
                open_from[client] = open_from[upstream] = False
                break
            if not broken:
                try:
                    feed[sock](data)
                except Exception as e:  # noqa: BLE001 (a record lost, never a byte)
                    broken = True
                    emit({"proto": getattr(recorder, "proto", None), "event": "parse_error",
                          "listener": recorder.listener, "time": now(),
                          "error": f"{type(e).__name__}: {e}"}, recorder.out)
    if not broken:
        recorder.closed()
    for sock in (client, upstream):
        try:
            sock.close()
        except OSError:
            pass


def _read_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("closed during negotiation")
        data += chunk
    return data


def pg_negotiate(client, upstream, server_tls, client_tls):
    """Handles SSLRequest and GSSENCRequest before the startup message. Returns (client,
    upstream, the first 8 bytes of the next message, whether TLS is on)."""
    tls = False
    while True:
        first = _read_exact(client, 8)
        (code,) = struct.unpack("!I", first[4:8])
        if code == GSSENC_REQUEST:
            client.sendall(b"N")
            continue
        if code != SSL_REQUEST or tls:
            return client, upstream, first, tls
        if server_tls is None:  # no certificate: the client goes on without TLS, or leaves
            client.sendall(b"N")
            continue
        upstream.sendall(first)
        answer = _read_exact(upstream, 1)
        client.sendall(answer)
        if answer == b"S":
            upstream = client_tls.wrap_socket(upstream)
            client = server_tls.wrap_socket(client, server_side=True)
            tls = True


def handle(kind, listener, conn, target, server_tls, client_tls, out):
    try:
        upstream = socket.create_connection(target, timeout=10)
        upstream.settimeout(None)
        conn.settimeout(None)
    except OSError as e:
        emit({"proto": kind, "event": "upstream_unreachable", "listener": listener,
              "time": now(), "error": str(e)}, out)
        conn.close()
        return
    if kind == "http":
        recorder = HttpRecorder(listener, out)
        recorder.proto = "http"
        relay(conn, upstream, recorder)
        return
    try:
        conn, upstream, first, tls = pg_negotiate(conn, upstream, server_tls, client_tls)
    except (OSError, ConnectionError, ssl.SSLError) as e:
        emit({"proto": "pg", "event": "negotiation_failed", "listener": listener, "time": now(),
              "error": str(e)}, out)
        conn.close()
        upstream.close()
        return
    recorder = PgRecorder(listener, tls, out)
    recorder.proto = "pg"
    upstream.sendall(first)
    recorder.client_bytes(first)
    relay(conn, upstream, recorder)


def serve(kind, listener, port, target, server_tls=None, client_tls=None, out=None,
          host="0.0.0.0", ready=None):
    """Accepts connections on `port` and relays each to `target` in its own thread."""
    sock = socket.create_server((host, port), reuse_port=False)
    if ready is not None:
        ready.append(sock.getsockname()[1])
    while True:
        conn, _ = sock.accept()
        threading.Thread(target=handle, daemon=True,
                         args=(kind, listener, conn, target, server_tls, client_tls,
                               out)).start()


def parse_listener(text):
    name, rest = text.split("=", 1)
    port, host, upstream_port = rest.split(":")
    return name, int(port), (host, int(upstream_port))


def tls_contexts(cert, key):
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client.check_hostname = False
    client.verify_mode = ssl.CERT_NONE  # the gate's own PostgreSQL; the client side is ours
    return server, client


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--http", action="append", default=[])
    parser.add_argument("--pg", action="append", default=[])
    parser.add_argument("--tls-cert")
    parser.add_argument("--tls-key")
    args = parser.parse_args(argv)
    server_tls, client_tls = (tls_contexts(args.tls_cert, args.tls_key)
                              if args.tls_cert else (None, None))
    threads = []
    for kind, specs in (("http", args.http), ("pg", args.pg)):
        for spec in specs:
            name, port, target = parse_listener(spec)
            threads.append(threading.Thread(
                target=serve, daemon=True,
                args=(kind, name, port, target, server_tls, client_tls)))
            emit({"event": "listening", "proto": kind, "listener": name, "port": port,
                  "upstream": f"{target[0]}:{target[1]}", "time": now()})
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
