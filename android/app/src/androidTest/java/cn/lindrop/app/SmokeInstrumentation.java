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
              + " rejection\n");
      finish(-1, result);
    } catch (Throwable error) {
      result.putString("stream", "ANDROID_SMOKE_FAILED: " + error + "\n");
      finish(0, result);
    }
  }
}
