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
              + " rejection, original-name receive commit and collision protection\n");
      finish(-1, result);
    } catch (Throwable error) {
      result.putString("stream", "ANDROID_SMOKE_FAILED: " + error + "\n");
      finish(0, result);
    }
  }
}
