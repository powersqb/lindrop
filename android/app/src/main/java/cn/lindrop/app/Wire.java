package cn.lindrop.app;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import javax.net.ssl.*;
import org.json.*;

/** Small HTTP/1.1 transport: numeric LAN endpoints only, no proxy/redirect, exact TLS pin. */
public final class Wire {
  public static class Head {
    public String first;
    public final Map<String, String> headers = new HashMap<>();

    public long length() throws IOException {
      String s = headers.get("content-length");
      if (s == null || !s.matches("[0-9]{1,12}")) throw new IOException("缺少有效 Content-Length");
      return Long.parseLong(s);
    }
  }

  static String line(InputStream in, int max) throws IOException {
    ByteArrayOutputStream b = new ByteArrayOutputStream();
    int c;
    while ((c = in.read()) != -1) {
      if (c == 10) {
        byte[] v = b.toByteArray();
        if (v.length == 0 || v[v.length - 1] != 13) throw new IOException("HTTP 换行无效");
        return new String(v, 0, v.length - 1, StandardCharsets.US_ASCII);
      }
      b.write(c);
      if (b.size() > max) throw new IOException("HTTP 头过长");
    }
    throw new EOFException("连接已中断");
  }

  public static Head head(InputStream in) throws IOException {
    Head h = new Head();
    h.first = line(in, 2048);
    int total = h.first.length();
    for (int i = 0; i < 40; i++) {
      String l = line(in, 4096);
      total += l.length();
      if (total > 16384) throw new IOException("HTTP 头过长");
      if (l.isEmpty()) {
        if (h.headers.containsKey("transfer-encoding")) throw new IOException("不支持分块编码");
        return h;
      }
      int p = l.indexOf(':');
      if (p < 1) throw new IOException("HTTP 头无效");
      String k = l.substring(0, p).toLowerCase(Locale.ROOT), v = l.substring(p + 1).trim();
      if (h.headers.put(k, v) != null) throw new IOException("HTTP 头重复");
    }
    throw new IOException("HTTP 头过多");
  }

  public static byte[] body(InputStream in, long n, int max) throws IOException {
    if (n < 0 || n > max) throw new IOException("消息过大");
    byte[] b = new byte[(int) n];
    int pos = 0;
    while (pos < b.length) {
      int got = in.read(b, pos, b.length - pos);
      if (got < 0) throw new EOFException();
      pos += got;
    }
    return b;
  }

  public static void respond(OutputStream out, int status, JSONObject j) throws IOException {
    byte[] b = j.toString().getBytes(StandardCharsets.UTF_8);
    out.write(
        ("HTTP/1.1 "
                + status
                + " Result\r\nContent-Type: application/json\r\nContent-Length: "
                + b.length
                + "\r\nConnection: close\r\n\r\n")
            .getBytes(StandardCharsets.US_ASCII));
    out.write(b);
    out.flush();
  }

  public static SSLSocket connect(SSLContext context, Protocol.Peer p, String source)
      throws Exception {
    if (!Protocol.lan(p.host)) throw new IOException("仅支持局域网");
    Socket raw = new Socket();
    try {
      raw.bind(new InetSocketAddress(source, 0));
      raw.connect(new InetSocketAddress(p.host, p.port), 5000);
      raw.setSoTimeout(30000);
      SSLSocket s = (SSLSocket) context.getSocketFactory().createSocket(raw, p.host, p.port, true);
      s.setEnabledProtocols(new String[] {"TLSv1.2"});
      s.startHandshake();
      String actual = Protocol.sha(s.getSession().getPeerCertificates()[0].getEncoded());
      if (!Protocol.equal(actual, p.fp)) throw new SSLException("设备证书变化，已阻止连接");
      return s;
    } catch (Exception e) {
      raw.close();
      throw e;
    }
  }

  public static void requestHead(
      OutputStream out,
      Protocol.Peer p,
      String method,
      String path,
      long len,
      String header,
      String secret)
      throws IOException {
    String authority = p.host + ":" + p.port;
    String h =
        method
            + " "
            + path
            + " HTTP/1.1\r\nHost: "
            + authority
            + "\r\nOrigin: https://"
            + authority
            + "\r\nContent-Type: application/json\r\nContent-Length: "
            + len
            + "\r\nConnection: close\r\n";
    if (secret != null) {
      if (!secret.matches("[A-Za-z0-9_-]{30,64}")) throw new IOException("授权无效");
      h += header + ": " + secret + "\r\n";
    }
    out.write((h + "\r\n").getBytes(StandardCharsets.US_ASCII));
  }

  public static JSONObject response(InputStream in) throws Exception {
    Head h = head(in);
    String[] p = h.first.split(" ");
    if (p.length < 2 || !p[1].matches("[0-9]{3}")) throw new IOException("响应无效");
    JSONObject j = new JSONObject(new String(body(in, h.length(), 65536), StandardCharsets.UTF_8));
    if (Integer.parseInt(p[1]) >= 400) throw new IOException(j.optString("error", "设备拒绝请求"));
    return j;
  }

  public static JSONObject request(
      SSLContext ctx,
      Protocol.Peer p,
      String source,
      String method,
      String path,
      JSONObject data,
      String secret)
      throws Exception {
    byte[] b = data == null ? new byte[0] : data.toString().getBytes(StandardCharsets.UTF_8);
    try (SSLSocket s = connect(ctx, p, source)) {
      requestHead(s.getOutputStream(), p, method, path, b.length, "X-Offer-Secret", secret);
      s.getOutputStream().write(b);
      s.getOutputStream().flush();
      return response(s.getInputStream());
    }
  }
}
