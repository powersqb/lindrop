package cn.lindrop.app;

import java.io.IOException;
import javax.net.ssl.SSLContext;
import org.json.JSONObject;

/** Previously human-verified endpoints only; never scan addresses or learn a new pin. */
public final class KnownPeers {
  private KnownPeers() {}

  public static JSONObject encode(Protocol.Peer p) throws Exception {
    return new JSONObject()
        .put("id", p.id)
        .put("name", p.name)
        .put("host", p.host)
        .put("port", p.port)
        .put("fingerprint", p.fp);
  }

  public static Protocol.Peer decode(String value) throws Exception {
    JSONObject j = new JSONObject(value);
    return new Protocol.Peer(j, j.getString("host"));
  }

  public static Protocol.Peer probe(SSLContext tls, Protocol.Peer saved, String source)
      throws Exception {
    JSONObject hello = Wire.request(tls, saved, source, "GET", "/peer/hello", null, null, 3000);
    if (!hello.optString("app").equals("lindrop")
        || hello.optInt("protocol") != 2
        || !hello.optString("id").equals(saved.id)
        || !Protocol.equal(hello.optString("fingerprint"), saved.fp))
      throw new IOException("已记住的设备身份不一致");
    // Keep the address and port we actually verified, not remote endpoint hints.
    hello.put("port", saved.port);
    return new Protocol.Peer(hello, saved.host);
  }
}
