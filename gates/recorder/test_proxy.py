"""Tests for the recorder (ADR-034), run against local fakes: an HTTP/1.1 server, and a
PostgreSQL server that speaks just enough of the wire protocol, TLS included.
Run: python3 -m unittest discover -s gates"""

import http.client
import http.server
import json
import os
import socket
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest

from recorder import proxy


class Out:
    """Collects the recorder's JSON lines."""

    def __init__(self):
        self.lines, self.lock = [], threading.Lock()

    def write(self, text):
        with self.lock:
            self.lines.append(text)

    def flush(self):
        pass

    def records(self, at_least=1, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                text = "".join(self.lines)
            records = [json.loads(line) for line in text.splitlines()]
            if len(records) >= at_least:
                return records
            time.sleep(0.02)
        raise AssertionError(f"{len(records)} records, wanted {at_least}: {records}")

    def text(self):
        with self.lock:
            return "".join(self.lines)


def start_recorder(kind, target, out, server_tls=None, client_tls=None):
    ready = []
    threading.Thread(target=proxy.serve, daemon=True,
                     args=(kind, "test", 0, target, server_tls, client_tls, out, "127.0.0.1",
                           ready)).start()
    deadline = time.monotonic() + 5
    while not ready and time.monotonic() < deadline:
        time.sleep(0.01)
    return ready[0]


# --- HTTP ---------------------------------------------------------------------------------

class Upstream(http.server.BaseHTTPRequestHandler):
    """Echoes what it received: GET/HEAD answer JSON, PUT/POST store the decoded body."""

    protocol_version = "HTTP/1.1"
    seen = []

    def body(self):
        if "chunked" in self.headers.get("Transfer-Encoding", ""):
            data = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0], 16)
                if not size:
                    self.rfile.readline()
                    return data
                data += self.rfile.read(size)
                self.rfile.readline()
        return self.rfile.read(int(self.headers.get("Content-Length", 0)))

    def answer(self, code, body, content_type="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "session=upstream-cookie")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):
        self.seen.append((self.command, self.path, dict(self.headers), b""))
        self.answer(200, json.dumps({"result": {"allow": True}}).encode())

    do_HEAD = do_GET

    def do_PUT(self):
        data = self.body()
        self.seen.append((self.command, self.path, dict(self.headers), data))
        self.answer(200, b"", "text/plain")

    def do_POST(self):
        data = self.body()
        self.seen.append((self.command, self.path, dict(self.headers), data))
        self.answer(200, json.dumps({"result": {"allow": False}}).encode())

    def log_message(self, *args):
        pass


class Http(unittest.TestCase):
    def setUp(self):
        Upstream.seen = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.out = Out()
        self.port = start_recorder("http", ("127.0.0.1", self.server.server_port), self.out)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def client(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_every_header_reaches_the_upstream_unchanged_and_secrets_stay_out_of_the_record(self):
        c = self.client()
        c.request("GET", "/v1/data/polaris/authz", headers={
            "Host": "signed-host.example:8333", "Authorization": "AWS4-HMAC-SHA256 sig=abc",
            "x-amz-security-token": "tok123"})
        response = c.getresponse()
        self.assertEqual(json.loads(response.read()), {"result": {"allow": True}})
        _, _, headers, _ = Upstream.seen[0]
        self.assertEqual(headers["Host"], "signed-host.example:8333")
        self.assertEqual(headers["Authorization"], "AWS4-HMAC-SHA256 sig=abc")
        self.assertEqual(headers["x-amz-security-token"], "tok123")
        (record,) = self.out.records()
        self.assertEqual((record["method"], record["path"], record["status"]),
                         ("GET", "/v1/data/polaris/authz", 200))
        self.assertEqual(record["request_headers"]["host"], "signed-host.example:8333")
        self.assertEqual(record["request_headers"]["authorization"], "<redacted>")
        self.assertEqual(record["response_headers"]["set-cookie"], "<redacted>")
        for secret in ("sig=abc", "tok123", "upstream-cookie"):
            self.assertNotIn(secret, self.out.text())

    def test_a_json_body_is_recorded_in_full_and_any_other_as_length_and_hash(self):
        c = self.client()
        c.request("POST", "/v1/data/polaris/authz", body=json.dumps({"input": {"a": 1}}),
                  headers={"Content-Type": "application/json"})
        c.getresponse().read()
        c.request("PUT", "/bucket/key", body=b"\x00binary", headers={
            "Content-Type": "application/octet-stream"})
        c.getresponse().read()
        post, put = self.out.records(2)
        self.assertEqual(post["request_body"]["json"], {"input": {"a": 1}})
        self.assertEqual(post["response_body"]["json"], {"result": {"allow": False}})
        self.assertEqual(put["request_body"]["length"], 7)
        self.assertRegex(put["request_body"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("json", put["request_body"])

    def test_query_credentials_are_forwarded_but_redacted_in_the_record(self):
        c = self.client()
        c.request("GET", "/b/k?X-Amz-Signature=deadbeef&X-Amz-Credential=AKID%2F1&part=2")
        c.getresponse().read()
        self.assertIn("X-Amz-Signature=deadbeef", Upstream.seen[0][1])
        (record,) = self.out.records()
        self.assertEqual(record["query"], {"X-Amz-Signature": "<redacted>",
                                           "X-Amz-Credential": "<redacted>", "part": "2"})
        self.assertNotIn("deadbeef", self.out.text())

    def test_expect_100_continue_with_a_chunked_upload_passes_through(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(b"PUT /bucket/big HTTP/1.1\r\nHost: s3\r\nExpect: 100-continue\r\n"
                  b"Transfer-Encoding: chunked\r\nContent-Type: application/octet-stream\r\n\r\n")
        interim = s.recv(4096)
        self.assertTrue(interim.startswith(b"HTTP/1.1 100"), interim)
        s.sendall(b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
        final = b""
        while b"\r\n\r\n" not in final:
            final += s.recv(4096)
        self.assertTrue(final.startswith(b"HTTP/1.0 200") or final.startswith(b"HTTP/1.1 200"))
        s.close()
        self.assertEqual(Upstream.seen[0][3], b"hello world")
        (record,) = self.out.records()
        self.assertEqual((record["method"], record["status"]), ("PUT", 200))
        self.assertEqual(record["request_body"]["length"], 11)

    def test_two_exchanges_on_one_kept_alive_connection_give_two_records(self):
        c = self.client()
        for path in ("/one", "/two"):
            c.request("GET", path)
            c.getresponse().read()
        self.assertEqual([r["path"] for r in self.out.records(2)], ["/one", "/two"])

    def test_a_head_response_has_no_body_and_the_next_exchange_still_parses(self):
        c = self.client()
        c.request("HEAD", "/b/k")
        c.getresponse().read()
        c.request("GET", "/after")
        c.getresponse().read()
        head, after = self.out.records(2)
        self.assertEqual((head["method"], head["response_body"]["length"]), ("HEAD", 0))
        self.assertEqual(after["path"], "/after")

    def test_bytes_the_recorder_cannot_parse_are_still_forwarded(self):
        raw = socket.create_server(("127.0.0.1", 0))
        got = []

        def upstream():
            conn, _ = raw.accept()
            got.append(conn.recv(4096))
            conn.sendall(b"pong")
            conn.close()
        threading.Thread(target=upstream, daemon=True).start()
        out = Out()
        port = start_recorder("http", raw.getsockname(), out)
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        s.sendall(b"NOT HTTP AT ALL\r\nbroken\r\n\r\n")
        self.assertEqual(s.recv(4096), b"pong")
        s.close()
        raw.close()
        self.assertEqual(got, [b"NOT HTTP AT ALL\r\nbroken\r\n\r\n"])
        self.assertEqual(out.records()[0]["event"], "parse_error")


# --- PostgreSQL ---------------------------------------------------------------------------

def msg(kind, payload=b""):
    return kind.encode() + struct.pack("!I", len(payload) + 4) + payload


def cstr(text):
    return text.encode() + b"\0"


def startup_message(password_user="polaris"):
    body = (struct.pack("!I", 196608) + cstr("user") + cstr(password_user) + cstr("database")
            + cstr("polaris") + cstr("application_name") + cstr("gate") + b"\0")
    return struct.pack("!I", len(body) + 4) + body


def read_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return data


def read_message(sock):
    head = read_exact(sock, 5)
    (length,) = struct.unpack("!I", head[1:5])
    return chr(head[0]), read_exact(sock, length - 4)


def until_ready(sock):
    kinds = []
    while True:
        kind, payload = read_message(sock)
        kinds.append((kind, payload))
        if kind == "Z":
            return kinds


ROW_DESC = msg("T", struct.pack("!H", 1) + cstr("secret_hash") + b"\0" * 18)
ERROR = msg("E", b"SERROR\0VERROR\0C23505\0Mduplicate key value\0"
                 b"DKey (name)=(top-secret-key) already exists.\0\0")


class PgUpstream:
    """Answers a login (asking for a cleartext password), then simple and extended queries:
    a query containing `fail` errors; any other returns one row holding `stored-hash-value`."""

    def __init__(self, tls_ctx=None, fail_login=False):
        self.tls_ctx, self.fail_login, self.plaintext = tls_ctx, fail_login, []
        self.sock = socket.create_server(("127.0.0.1", 0))
        threading.Thread(target=self.serve, daemon=True).start()

    def address(self):
        return self.sock.getsockname()

    def serve(self):
        conn, _ = self.sock.accept()
        try:
            self.session(conn)
        except (ConnectionError, OSError):
            pass

    def session(self, conn):
        first = read_exact(conn, 8)
        if struct.unpack("!I", first[4:8])[0] == proxy.SSL_REQUEST:
            if self.tls_ctx:
                conn.sendall(b"S")
                conn = self.tls_ctx.wrap_socket(conn, server_side=True)
            else:
                conn.sendall(b"N")
            first = read_exact(conn, 8)
        read_exact(conn, struct.unpack("!I", first[:4])[0] - 8)
        conn.sendall(msg("R", struct.pack("!I", 3)))
        kind, payload = read_message(conn)
        self.plaintext.append((kind, payload))
        if self.fail_login:
            conn.sendall(msg("E", b"SFATAL\0VFATAL\0C28P01\0Mpassword authentication failed\0\0"))
            conn.close()
            return
        conn.sendall(msg("R", struct.pack("!I", 0)) + msg("S", cstr("server_version") + cstr("16"))
                     + msg("K", struct.pack("!II", 1, 2)) + msg("Z", b"I"))
        failing = False
        while True:
            kind, payload = read_message(conn)
            self.plaintext.append((kind, payload))
            if kind == "X":
                conn.close()
                return
            if kind == "Q":
                if b"fail" in payload:
                    conn.sendall(ERROR + msg("Z", b"I"))
                else:
                    conn.sendall(ROW_DESC + msg("D", struct.pack("!HI", 1, 17) + b"stored-hash-value")
                                 + msg("C", cstr("SELECT 1")) + msg("Z", b"I"))
            elif kind == "P":
                failing = b"fail" in payload
                conn.sendall(msg("1"))
            elif kind == "B":
                conn.sendall(msg("2"))
            elif kind == "E":
                conn.sendall(ERROR if failing else
                             msg("D", struct.pack("!HI", 1, 17) + b"stored-hash-value")
                             + msg("C", cstr("SELECT 1")))
            elif kind == "S":
                conn.sendall(msg("Z", b"I"))


def make_cert(directory):
    cert, key = os.path.join(directory, "c.pem"), os.path.join(directory, "k.pem")
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=test", "-keyout", key, "-out", cert],
                   check=True, capture_output=True)
    return cert, key


def login(sock, password="pw-not-recorded"):
    sock.sendall(startup_message())
    kind, _ = read_message(sock)
    assert kind == "R"
    sock.sendall(msg("p", cstr(password)))
    return until_ready(sock)


class Pg(unittest.TestCase):
    def connect(self, upstream, out, tls_files=None, ask_tls=False):
        server_tls = client_tls = None
        if tls_files:
            server_tls, client_tls = proxy.tls_contexts(*tls_files)
        port = start_recorder("pg", upstream.address(), out, server_tls, client_tls)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        if ask_tls:
            sock.sendall(struct.pack("!II", 8, proxy.SSL_REQUEST))
            answer = sock.recv(1)
            if answer == b"S":
                ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
                sock = ctx.wrap_socket(sock)
            return sock, answer
        return sock, None

    def test_a_simple_query_is_recorded_with_columns_rows_and_tag_never_values(self):
        upstream, out = PgUpstream(), Out()
        sock, _ = self.connect(upstream, out)
        login(sock)
        sock.sendall(msg("Q", cstr("SELECT secret_hash FROM principal_authentication_data")))
        until_ready(sock)
        sock.sendall(msg("X"))
        connect, statement = out.records(2)
        self.assertEqual((connect["event"], connect["result"], connect["user"],
                          connect["database"], connect["application_name"], connect["tls"]),
                         ("connect", "ok", "polaris", "polaris", "gate", False))
        self.assertEqual(statement["sql"], "SELECT secret_hash FROM principal_authentication_data")
        self.assertEqual((statement["columns"], statement["rows"], statement["tag"]),
                         (["secret_hash"], 1, "SELECT 1"))
        for secret in ("stored-hash-value", "pw-not-recorded"):
            self.assertNotIn(secret, out.text())

    def test_an_extended_query_records_how_many_parameters_never_their_values(self):
        upstream, out = PgUpstream(), Out()
        sock, _ = self.connect(upstream, out)
        login(sock)
        sql = "SELECT * FROM principal_authentication_data WHERE secret_hash = $1"
        sock.sendall(msg("P", cstr("") + cstr(sql) + struct.pack("!H", 0))
                     + msg("B", cstr("") + cstr("") + struct.pack("!HHI", 0, 1, 9) + b"param-val"
                           + struct.pack("!H", 0))
                     + msg("E", cstr("") + struct.pack("!I", 0)) + msg("S"))
        until_ready(sock)
        _, statement = out.records(2)
        self.assertEqual((statement["sql"], statement["params"], statement["rows"],
                          statement["tag"]), (sql, 1, 1, "SELECT 1"))
        self.assertEqual(upstream.plaintext[-3][0], "B")
        self.assertIn(b"param-val", upstream.plaintext[-3][1])
        for secret in ("param-val", "stored-hash-value"):
            self.assertNotIn(secret, out.text())

    def test_an_error_is_recorded_with_its_sqlstate_never_its_detail(self):
        upstream, out = PgUpstream(), Out()
        sock, _ = self.connect(upstream, out)
        login(sock)
        sock.sendall(msg("Q", cstr("INSERT INTO entities VALUES ('fail')")))
        until_ready(sock)
        _, statement = out.records(2)
        self.assertEqual(statement["error"], {"severity": "ERROR", "sqlstate": "23505",
                                              "message": "duplicate key value"})
        self.assertNotIn("top-secret-key", out.text())

    def test_a_failed_login_is_one_connect_record_with_its_sqlstate(self):
        upstream, out = PgUpstream(fail_login=True), Out()
        sock, _ = self.connect(upstream, out)
        sock.sendall(startup_message())
        read_message(sock)
        sock.sendall(msg("p", cstr("wrong-password")))
        kind, _ = read_message(sock)
        self.assertEqual(kind, "E")
        (record,) = out.records()
        self.assertEqual((record["event"], record["result"], record["error"]["sqlstate"]),
                         ("connect", "error", "28P01"))
        self.assertNotIn("wrong-password", out.text())

    def test_tls_is_accepted_from_the_client_and_opened_again_to_the_upstream(self):
        with tempfile.TemporaryDirectory() as d:
            for sub in ("u", "r"):
                os.makedirs(os.path.join(d, sub))
            upstream_files = make_cert(os.path.join(d, "u"))
            recorder_files = make_cert(os.path.join(d, "r"))
            upstream_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            upstream_ctx.load_cert_chain(*upstream_files)
            upstream, out = PgUpstream(tls_ctx=upstream_ctx), Out()
            sock, answer = self.connect(upstream, out, tls_files=recorder_files, ask_tls=True)
            self.assertEqual(answer, b"S")
            self.assertIsInstance(sock, ssl.SSLSocket)
            login(sock)
            sock.sendall(msg("Q", cstr("SELECT 1")))
            until_ready(sock)
            connect, statement = out.records(2)
        self.assertTrue(connect["tls"])
        self.assertEqual(statement["sql"], "SELECT 1")
        self.assertIn(("Q", cstr("SELECT 1")), upstream.plaintext)

    def test_without_a_certificate_a_tls_request_is_refused_and_the_login_goes_on_plain(self):
        upstream, out = PgUpstream(), Out()
        sock, answer = self.connect(upstream, out, ask_tls=True)
        self.assertEqual(answer, b"N")
        login(sock)
        (connect,) = out.records()
        self.assertFalse(connect["tls"])

    def test_a_gssapi_encryption_request_is_refused(self):
        upstream, out = PgUpstream(), Out()
        port = start_recorder("pg", upstream.address(), out)
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(struct.pack("!II", 8, proxy.GSSENC_REQUEST))
        self.assertEqual(sock.recv(1), b"N")
        login(sock)
        self.assertEqual(out.records()[0]["result"], "ok")


class Listener(unittest.TestCase):
    def test_a_listener_is_name_port_host_and_upstream_port(self):
        self.assertEqual(proxy.parse_listener("opa=8181:opa.standins.svc:8181"),
                         ("opa", 8181, ("opa.standins.svc", 8181)))


if __name__ == "__main__":
    unittest.main()
