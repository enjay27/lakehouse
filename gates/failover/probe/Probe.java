// The failover probe (ADR-047 §5). It writes one row every PROBE_PERIOD_MS, in two modes side
// by side, with pgjdbc, the driver Polaris uses:
//   pooled     one long-lived connection, reused, and replaced after a failure, as a pool does;
//   per_write  a new connection for each write.
// Each attempt is one JSON line on stdout: mode, seq, sent and done (epoch ms), ok, and the
// error when it failed. The row holds the run, the mode, the sequence number and the send
// time, so that a write whose commit returned can be looked for once every instance is back.
//
// Environment: PROBE_URL (a JDBC URL without credentials), PGUSER, PGPASSWORD, PROBE_RUN,
// PROBE_PERIOD_MS. It runs until it is stopped.

import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.PreparedStatement;
import java.sql.SQLException;
import java.sql.Statement;
import java.util.Properties;

public class Probe {
    static final String TABLE = "CREATE TABLE IF NOT EXISTS probe_writes (run text NOT NULL, "
            + "mode text NOT NULL, seq bigint NOT NULL, sent bigint NOT NULL, "
            + "PRIMARY KEY (run, mode, seq))";
    static final String INSERT = "INSERT INTO probe_writes (run, mode, seq, sent) VALUES (?, ?, ?, ?)";

    final String url;
    final Properties login = new Properties();
    final String run;
    final long period;

    Probe(String url, String user, String password, String run, long period) {
        this.url = url;
        this.login.setProperty("user", user);
        this.login.setProperty("password", password);
        this.login.setProperty("ApplicationName", "failover-probe");
        this.run = run;
        this.period = period;
    }

    public static void main(String[] args) throws Exception {
        Probe probe = new Probe(need("PROBE_URL"), need("PGUSER"), need("PGPASSWORD"),
                need("PROBE_RUN"), Long.parseLong(need("PROBE_PERIOD_MS")));
        probe.createTable();
        emit("{\"event\":\"ready\",\"run\":" + quote(probe.run) + "}");
        Thread pooled = new Thread(probe::pooled, "pooled");
        Thread perWrite = new Thread(probe::perWrite, "per_write");
        pooled.start();
        perWrite.start();
        pooled.join();
        perWrite.join();
    }

    static String need(String name) {
        String value = System.getenv(name);
        if (value == null || value.isEmpty()) {
            throw new IllegalStateException(name + " is not set");
        }
        return value;
    }

    // The table is made before any write is timed; until PostgreSQL answers, it retries.
    void createTable() throws InterruptedException {
        while (true) {
            try (Connection c = DriverManager.getConnection(url, login);
                    Statement s = c.createStatement()) {
                s.execute(TABLE);
                return;
            } catch (SQLException e) {
                emit("{\"event\":\"waiting\",\"error\":" + quote(describe(e)) + "}");
                Thread.sleep(1000);
            }
        }
    }

    void pooled() {
        Connection connection = null;
        long next = System.currentTimeMillis();
        for (long seq = 0; ; seq++) {
            next = waitFor(next);
            long sent = System.currentTimeMillis();
            try {
                if (connection == null) {
                    connection = DriverManager.getConnection(url, login);
                }
                insert(connection, "pooled", seq, sent);
                record("pooled", seq, sent, null);
            } catch (Exception e) {
                record("pooled", seq, sent, describe(e));
                close(connection);
                connection = null;
            }
        }
    }

    void perWrite() {
        long next = System.currentTimeMillis();
        for (long seq = 0; ; seq++) {
            next = waitFor(next);
            long sent = System.currentTimeMillis();
            try (Connection connection = DriverManager.getConnection(url, login)) {
                insert(connection, "per_write", seq, sent);
                record("per_write", seq, sent, null);
            } catch (Exception e) {
                record("per_write", seq, sent, describe(e));
            }
        }
    }

    void insert(Connection connection, String mode, long seq, long sent) throws SQLException {
        try (PreparedStatement p = connection.prepareStatement(INSERT)) {
            p.setString(1, run);
            p.setString(2, mode);
            p.setLong(3, seq);
            p.setLong(4, sent);
            p.executeUpdate();
        }
    }

    // Sleeps until `next`, then returns the tick after it. A write that overran its tick moves
    // the schedule on instead of firing the missed ticks at once.
    long waitFor(long next) {
        long now = System.currentTimeMillis();
        if (next > now) {
            try {
                Thread.sleep(next - now);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
            }
            now = next;
        }
        return Math.max(next, now) + period;
    }

    static void record(String mode, long seq, long sent, String error) {
        long done = System.currentTimeMillis();
        StringBuilder line = new StringBuilder(160)
                .append("{\"mode\":").append(quote(mode))
                .append(",\"seq\":").append(seq)
                .append(",\"sent\":").append(sent)
                .append(",\"done\":").append(done)
                .append(",\"ok\":").append(error == null);
        if (error != null) {
            line.append(",\"error\":").append(quote(error));
        }
        emit(line.append('}').toString());
    }

    static void close(Connection connection) {
        if (connection != null) {
            try {
                connection.close();
            } catch (SQLException ignored) {
                // Already broken: nothing to close.
            }
        }
    }

    static String describe(Exception e) {
        String state = e instanceof SQLException s && s.getSQLState() != null ? s.getSQLState() + " " : "";
        String message = String.valueOf(e.getMessage());
        if (message.length() > 200) {
            message = message.substring(0, 200);
        }
        return state + e.getClass().getSimpleName() + ": " + message;
    }

    static synchronized void emit(String line) {
        System.out.println(line);
        System.out.flush();
    }

    static String quote(String s) {
        StringBuilder out = new StringBuilder(s.length() + 2).append('"');
        for (char ch : s.toCharArray()) {
            switch (ch) {
                case '"' -> out.append("\\\"");
                case '\\' -> out.append("\\\\");
                case '\n' -> out.append("\\n");
                case '\r' -> out.append("\\r");
                case '\t' -> out.append("\\t");
                default -> {
                    if (ch < 0x20) {
                        out.append(String.format("\\u%04x", (int) ch));
                    } else {
                        out.append(ch);
                    }
                }
            }
        }
        return out.append('"').toString();
    }
}
