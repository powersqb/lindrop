package cn.lindrop.app;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.security.*;
import java.util.*;
import org.json.*;

/** Wire constants and strict validation shared by Android and the JVM interop tests. */
public final class Protocol {
  public static final long MAX_FILE = 2L * 1024 * 1024 * 1024;
  public static final String GROUP = "239.255.42.99";
  public static final int DISCOVERY_PORT = 45876;
  private static final SecureRandom RANDOM = new SecureRandom();

  public static String hex(byte[] bytes) {
    StringBuilder s = new StringBuilder();
    for (byte b : bytes) s.append(String.format(Locale.ROOT, "%02x", b & 255));
    return s.toString();
  }

  public static String random(int bytes) {
    byte[] b = new byte[bytes];
    RANDOM.nextBytes(b);
    return hex(b);
  }

  public static String sha(byte[] b) throws Exception {
    return hex(MessageDigest.getInstance("SHA-256").digest(b));
  }

  public static boolean equal(String a, String b) {
    return a != null
        && b != null
        && MessageDigest.isEqual(
            a.getBytes(StandardCharsets.UTF_8), b.getBytes(StandardCharsets.UTF_8));
  }

  public static boolean lan(
      String ip) { // Parse numeric IPv4 only; never resolve an untrusted hostname.
    String[] p = ip.split("\\.");
    if (p.length != 4) return false;
    int[] a = new int[4];
    try {
      for (int i = 0; i < 4; i++) {
        if (!p[i].matches("0|[1-9][0-9]{0,2}")) return false;
        a[i] = Integer.parseInt(p[i]);
        if (a[i] > 255) return false;
      }
    } catch (Exception e) {
      return false;
    }
    return a[0] == 10
        || a[0] == 127
        || (a[0] == 192 && a[1] == 168)
        || (a[0] == 172 && a[1] >= 16 && a[1] <= 31);
  }

  public static String name(String s) throws IOException {
    if (s == null
        || s.isEmpty()
        || s.getBytes(StandardCharsets.UTF_8).length > 200
        || s.matches(".*[/\\\\:<>\"|?*].*")
        || s.endsWith(".")
        || s.endsWith(" ")
        || s.equals(".")
        || s.equals("..")) throw new IOException("文件名不合法，请先重命名");
    for (char c : s.toCharArray()) if (c < 32 || c == 127) throw new IOException("文件名含控制字符");
    String stem = s.split("\\.", 2)[0].toUpperCase(Locale.ROOT);
    if (stem.matches("CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]")) throw new IOException("文件名不兼容 Windows");
    // Refuse rather than silently normalize: Windows protocol currently canonicalizes NFC.
    if (!java.text.Normalizer.isNormalized(s, java.text.Normalizer.Form.NFC))
      throw new IOException("文件名编码不兼容电脑端，请先重命名");
    return s;
  }

  public static String text(String s) throws IOException {
    if (s == null || s.trim().isEmpty() || s.getBytes(StandardCharsets.UTF_8).length > 4096)
      throw new IOException("消息不能为空，最多 4 KiB");
    for (char c : s.toCharArray())
      if (c < 32 && c != '\n' && c != '\t') throw new IOException("消息含控制字符");
    return s;
  }

  public static String code(String sender, String receiver, String nonce, String id)
      throws Exception {
    String s =
        sha((sender + receiver + nonce + id).getBytes(StandardCharsets.US_ASCII))
            .substring(0, 16)
            .toUpperCase(Locale.ROOT);
    return s.substring(0, 4)
        + "-"
        + s.substring(4, 8)
        + "-"
        + s.substring(8, 12)
        + "-"
        + s.substring(12);
  }

  public static void files(JSONArray files) throws Exception {
    if (files.length() < 1 || files.length() > 50) throw new IOException("每批 1 至 50 个文件");
    long sum = 0;
    Set<String> seen = new HashSet<>();
    for (int i = 0; i < files.length(); i++) {
      JSONObject f = files.getJSONObject(i);
      String n = name(f.getString("name"));
      Object size = f.get("size");
      if (!(size instanceof Integer || size instanceof Long)) throw new IOException("文件大小无效");
      long v = f.getLong("size");
      if (v < 0 || v > MAX_FILE || !f.getString("sha256").matches("[0-9a-f]{64}"))
        throw new IOException("文件信息无效");
      if (!seen.add(n.toLowerCase(Locale.ROOT))) throw new IOException("同批文件名重复，请分批发送");
      sum += v;
    }
    if (sum > 20L * 1024 * 1024 * 1024) throw new IOException("每批最多 20 GiB");
  }

  public static class Peer {
    public final String id, name, host, fp;
    public final int port;
    public volatile long seen;

    public Peer(JSONObject j, String host) throws Exception {
      this.id = j.getString("id");
      this.fp = j.getString("fingerprint");
      this.port = j.getInt("port");
      this.name = j.getString("name");
      this.host = host;
      seen = System.nanoTime() / 1000000;
      if (!lan(host)
          || !id.matches("[0-9a-f]{32}")
          || !fp.matches("[0-9a-f]{64}")
          || port < 1024
          || port > 65535
          || name.trim().isEmpty()
          || name.length() > 40) throw new IOException("设备信息无效");
      for (char c : name.toCharArray()) if (c < 32 || c == 127) throw new IOException("设备名无效");
    }

    @Override
    public String toString() {
      return name + " · " + host;
    }
  }
}
