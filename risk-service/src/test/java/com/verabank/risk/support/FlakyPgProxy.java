package com.verabank.risk.support;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.time.Duration;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;

/**
 * Proxy TCP hacia el PostgreSQL de pruebas que estropea sus primeras {@code affected} conexiones y reenvía las demás:
 * las corta nada más aceptarlas ({@link #rejecting}) o las retiene un rato antes de conectarlas ({@link #stalling}).
 * Reproduce un arranque en el que solo una parte del pool se abre a tiempo, como cuando la base todavía no estaba
 * lista (D16 en kind: risk-scoring arrancó antes que PostgreSQL).
 */
public final class FlakyPgProxy {

    private final ServerSocket server;
    private final AtomicInteger affected;
    private final Duration stall;   // cero: cortar
    private final ExecutorService threads = Executors.newCachedThreadPool(r -> {
        Thread t = new Thread(r, "flaky-pg-proxy");
        t.setDaemon(true);
        return t;
    });

    private FlakyPgProxy(int affected, Duration stall) {
        PgInfra.init();
        this.affected = new AtomicInteger(affected);
        this.stall = stall;
        try {
            this.server = new ServerSocket(0, 50, InetAddress.getLoopbackAddress());
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
        threads.submit(this::acceptLoop);
    }

    /** Corta las primeras {@code n} conexiones: el cliente las ve fallar. */
    public static FlakyPgProxy rejecting(int n) {
        return new FlakyPgProxy(n, Duration.ZERO);
    }

    /** Retiene las primeras {@code n} conexiones durante {@code stall} y después las conecta con normalidad. */
    public static FlakyPgProxy stalling(int n, Duration stall) {
        return new FlakyPgProxy(n, stall);
    }

    public String reactiveUrl() {
        return "postgresql://127.0.0.1:" + server.getLocalPort() + "/" + PgInfra.DB;
    }

    private void acceptLoop() {
        while (!server.isClosed()) {
            try {
                Socket client = server.accept();
                boolean spoiled = affected.getAndDecrement() > 0;
                if (spoiled && stall.isZero()) {
                    client.close();
                } else {
                    threads.submit(() -> forward(client, spoiled ? stall : Duration.ZERO));
                }
            } catch (IOException e) {
                // conexión perdida: el cliente lo ve como un fallo de conexión
            }
        }
    }

    private void forward(Socket client, Duration delay) {
        try {
            Thread.sleep(delay.toMillis());
            Socket upstream = new Socket(PgInfra.POSTGRES.getHost(), PgInfra.POSTGRES.getMappedPort(5432));
            pump(client, upstream);
            pump(upstream, client);
        } catch (IOException e) {
            closeQuietly(client);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            closeQuietly(client);
        }
    }

    private void pump(Socket from, Socket to) {
        threads.submit(() -> {
            try (InputStream in = from.getInputStream(); OutputStream out = to.getOutputStream()) {
                in.transferTo(out);
            } catch (IOException e) {
                // fin de la conexión en cualquiera de los dos lados
            } finally {
                closeQuietly(from);
                closeQuietly(to);
            }
        });
    }

    private static void closeQuietly(Socket socket) {
        try {
            socket.close();
        } catch (IOException e) {
            // ya cerrado
        }
    }
}
