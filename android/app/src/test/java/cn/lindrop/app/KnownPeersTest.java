package cn.lindrop.app;

import static org.junit.Assert.*;

import org.json.JSONObject;
import org.junit.Test;

public class KnownPeersTest {
  @Test
  public void endpointRoundTripPreservesIdentityAndUsesMilliseconds() throws Exception {
    JSONObject j =
        new JSONObject()
            .put("id", "a".repeat(32))
            .put("name", "电脑")
            .put("port", 8765)
            .put("fingerprint", "b".repeat(64));
    Protocol.Peer peer = new Protocol.Peer(j, "192.168.1.8");
    Protocol.Peer restored = KnownPeers.decode(KnownPeers.encode(peer).toString());
    assertEquals(peer.id, restored.id);
    assertEquals(peer.fp, restored.fp);
    assertEquals(peer.host, restored.host);
    assertEquals(peer.port, restored.port);
    assertTrue(Math.abs(System.nanoTime() / 1000000 - restored.seen) < 1000);
    j.put("host", "8.8.8.8");
    assertThrows(Exception.class, () -> KnownPeers.decode(j.toString()));
  }
}
