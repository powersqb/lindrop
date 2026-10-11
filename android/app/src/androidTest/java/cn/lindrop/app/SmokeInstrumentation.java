package cn.lindrop.app;

import android.app.*;
import android.content.Intent;
import android.os.Bundle;
import javax.net.ssl.*;
import org.json.*;

/** Real Android smoke test: UI starts, persistent Keystore identity, TLS server and pinning. */
public class SmokeInstrumentation extends Instrumentation {
  @Override
  public void onCreate(Bundle args) {
    super.onCreate(args);
    start();
  }

  @Override
  public void onStart() {
    Bundle result = new Bundle();
    try {
      MainActivity activity =
          (MainActivity)
              startActivitySync(
                  new Intent(getTargetContext(), MainActivity.class)
                      .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
      long end = System.currentTimeMillis() + 60000;
      int attempts = 0;
      while (activity.node == null && System.currentTimeMillis() < end) {
        Thread.sleep(1000);
        if (++attempts % 5 == 0) runOnMainSync(activity::startNode);
      }
      Node node = activity.node;
      if (node == null) {
        String[] status = {""};
        runOnMainSync(() -> status[0] = activity.status.getText().toString());
        throw new AssertionError("Node not started: " + status[0]);
      }
      while (node.port == 0 && System.currentTimeMillis() < end) Thread.sleep(200);
      Identity second = new Identity();
      if (!second.fingerprint.equals(node.fp))
        throw new AssertionError("Keystore identity changed");
      Protocol.Peer self = new Protocol.Peer(node.identity(), node.host);
      JSONObject hello =
          Wire.request(second.client, self, node.host, "GET", "/peer/hello", null, null);
      if (!hello.getString("id").equals(node.id)) throw new AssertionError("TLS hello mismatch");
      Protocol.Peer saved = KnownPeers.decode(KnownPeers.encode(self).toString());
      Node restored =
          new Node(
              second.server,
              second.client,
              "b".repeat(32),
              second.fingerprint,
              node.host,
              "Auto test",
              new java.io.File(getTargetContext().getFilesDir(), "auto-test"),
              new Node.Trust() {
                public String get(String id) {
                  return id.equals(saved.id) ? saved.fp : null;
                }

                public void remember(String id, String fp) {
                  throw new AssertionError("Auto trust");
                }

                public java.util.List<Protocol.Peer> endpoints() {
                  return java.util.List.of(saved);
                }
              },
              new Node.Ui() {
                public void peers(java.util.List<Protocol.Peer> peers) {}

                public void message(Protocol.Peer p, String text, boolean mine) {}

                public void status(String text) {}

                public boolean confirm(String title, String text) {
                  throw new AssertionError("Auto confirmation");
                }
              });
      try {
        restored.startSaved();
        long deadline = System.currentTimeMillis() + 10000;
        while (!restored.peers.containsKey(saved.id) && System.currentTimeMillis() < deadline)
          Thread.sleep(100);
        if (!restored.peers.containsKey(saved.id))
          throw new AssertionError("Saved endpoint not restored online");
        Protocol.Peer wrongId =
            new Protocol.Peer(KnownPeers.encode(saved).put("id", "c".repeat(32)), saved.host);
        try {
          KnownPeers.probe(second.client, wrongId, node.host);
          throw new AssertionError("Saved device ID mismatch accepted");
        } catch (java.io.IOException expected) {
        }
      } finally {
        restored.close();
      }
      Node announcer =
          new Node(
              second.server,
              second.client,
              "c".repeat(32),
              second.fingerprint,
              node.host,
              "首次自动发现测试",
              new java.io.File(getTargetContext().getFilesDir(), "presence-test"),
              restored.trust,
              restored.ui);
      try {
        announcer.start();
        announcer.multicast.close();
        node.peers.remove(announcer.id);
        JSONObject reply =
            Wire.request(
                second.client,
                self,
                node.host,
                "POST",
                "/peer/presence",
                announcer.identity().put("app", "lindrop").put("protocol", 2),
                null);
        if (!reply.optBoolean("discovered") || !node.peers.containsKey(announcer.id))
          throw new AssertionError("First TLS presence discovery failed");
        if (node.trust.get(announcer.id) != null)
          throw new AssertionError("Presence automatically trusted a new device");
      } finally {
        announcer.close();
        node.peers.remove(announcer.id);
        activity.peers(new java.util.ArrayList<>(node.peers.values()));
      }
      Protocol.Peer wrong =
          new Protocol.Peer(node.identity().put("fingerprint", "0".repeat(64)), node.host);
      try (SSLSocket ignored = Wire.connect(second.client, wrong, node.host)) {
        throw new AssertionError("Wrong certificate pin accepted");
      } catch (SSLException expected) {
      }
      java.io.File dir = new java.io.File(getTargetContext().getFilesDir(), "received-smoke");
      if (!dir.isDirectory() && !dir.mkdirs()) throw new AssertionError("Storage unavailable");
      java.io.File temp = new java.io.File(dir, ".receiving-test.part");
      java.io.File target = new java.io.File(dir, "原名.txt");
      byte[] bytes = {0, 1, 2, (byte) 255};
      try {
        java.nio.file.Files.write(temp.toPath(), bytes);
        ReceivedFiles.commit(temp, target, false);
        if (!java.util.Arrays.equals(bytes, java.nio.file.Files.readAllBytes(target.toPath())))
          throw new AssertionError("Received content changed");
        java.nio.file.Files.write(temp.toPath(), new byte[] {9});
        try {
          ReceivedFiles.commit(temp, target, false);
          throw new AssertionError("Late collision overwritten");
        } catch (java.io.IOException expected) {
        }
        if (!java.util.Arrays.equals(bytes, java.nio.file.Files.readAllBytes(target.toPath())))
          throw new AssertionError("Original damaged after collision");
        ReceivedFiles.commit(temp, target, true);
        if (!java.util.Arrays.equals(
            new byte[] {9}, java.nio.file.Files.readAllBytes(target.toPath())))
          throw new AssertionError("Overwrite failed");
      } finally {
        temp.delete();
        target.delete();
        dir.delete();
      }
      waitForIdleSync();
      android.graphics.Bitmap screen = getUiAutomation().takeScreenshot();
      if (screen != null) {
        try (java.io.FileOutputStream out =
            new java.io.FileOutputStream(
                new java.io.File(
                    getTargetContext().getExternalFilesDir(null), "smoke-screen.png"))) {
          screen.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, out);
        }
        screen.recycle();
      }
      result.putString(
          "stream",
          "ANDROID_SMOKE_OK: UI, Android Keystore persistence, TLS server/client, wrong-pin"
              + " rejection, original-name receive commit, collision protection, saved endpoint"
              + " automatic discovery and device ID mismatch rejection\n");
      finish(-1, result);
    } catch (Throwable error) {
      result.putString("stream", "ANDROID_SMOKE_FAILED: " + error + "\n");
      finish(0, result);
    }
  }
}
