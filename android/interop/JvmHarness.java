package cn.lindrop.app;

import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.*;
import java.security.cert.X509Certificate;
import java.util.*;
import javax.net.ssl.*;
import org.json.*;

/** Runs the exact Android protocol classes on a desktop JVM, without Android UI/Keystore. */
public final class JvmHarness {
  static synchronized void event(JSONObject j) {
    System.out.println(j);
    System.out.flush();
  }

  public static void main(String[] args) throws Exception {
    KeyStore keys = KeyStore.getInstance("PKCS12");
    try (InputStream in = new FileInputStream(args[0])) {
      keys.load(in, "interop-only".toCharArray());
    }
    KeyManagerFactory km = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm());
    km.init(keys, "interop-only".toCharArray());
    SSLContext server = SSLContext.getInstance("TLS");
    server.init(km.getKeyManagers(), null, null);
    SSLContext client = SSLContext.getInstance("TLS");
    client.init(
        null,
        new TrustManager[] {
          new X509TrustManager() {
            public X509Certificate[] getAcceptedIssuers() {
              return new X509Certificate[0];
            }

            public void checkClientTrusted(X509Certificate[] c, String a) {}

            public void checkServerTrusted(X509Certificate[] c, String a) {}
          }
        },
        null);
    String alias = keys.aliases().nextElement(),
        fp = Protocol.sha(keys.getCertificate(alias).getEncoded());
    Map<String, String> pins = new java.util.concurrent.ConcurrentHashMap<>();
    Node.Trust trust =
        new Node.Trust() {
          public String get(String id) {
            return pins.get(id);
          }

          public void remember(String id, String fp) {
            pins.put(id, fp);
          }
        };
    Node.Ui ui =
        new Node.Ui() {
          public void peers(List<Protocol.Peer> p) {}

          public void status(String s) {
            try {
              event(new JSONObject().put("event", "status").put("text", s));
            } catch (Exception ignored) {
            }
          }

          public void message(Protocol.Peer p, String s, boolean mine) {
            try {
              event(new JSONObject().put("event", "message").put("text", s).put("mine", mine));
            } catch (Exception ignored) {
            }
          }

          public boolean confirm(String title, String text) {
            try {
              event(new JSONObject().put("event", "confirm").put("title", title).put("text", text));
            } catch (Exception ignored) {
            }
            return true;
          }
        };
    Node node =
        new Node(
            server,
            client,
            Protocol.random(16),
            fp,
            "127.0.0.1",
            "Android JVM",
            new File(args[1]),
            trust,
            ui);
    node.start();
    event(node.identity().put("event", "ready"));
    try (BufferedReader input =
        new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8))) {
      String line;
      while ((line = input.readLine()) != null) {
        JSONObject cmd = new JSONObject(line);
        String action = cmd.getString("action");
        if (action.equals("close")) break;
        Protocol.Peer peer = new Protocol.Peer(cmd.getJSONObject("peer"), "127.0.0.1");
        if (action.equals("chat")) node.sendChat(peer, cmd.getString("text"));
        if (action.equals("files"))
          node.sendFiles(
              peer,
              List.of(new Node.SendFile(new File(cmd.getString("path")), cmd.getString("name"))));
        if (action.equals("wrong-pin")) {
          JSONObject p = cmd.getJSONObject("peer").put("fingerprint", "0".repeat(64));
          try {
            Wire.request(
                client,
                new Protocol.Peer(p, "127.0.0.1"),
                node.host,
                "GET",
                "/peer/hello",
                null,
                null);
            throw new AssertionError("Wrong pin accepted");
          } catch (SSLException expected) {
            event(new JSONObject().put("event", "wrong-pin-blocked"));
          }
        }
      }
    } finally {
      node.close();
    }
  }
}
