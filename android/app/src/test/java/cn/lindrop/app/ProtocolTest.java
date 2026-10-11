package cn.lindrop.app;

import static org.junit.Assert.*;

import java.io.*;
import java.nio.charset.StandardCharsets;
import org.json.*;
import org.junit.Test;

public class ProtocolTest {
  @Test
  public void numericLanOnly() {
    assertTrue(Protocol.lan("192.168.1.20"));
    assertTrue(Protocol.lan("172.31.255.254"));
    assertTrue(Protocol.lan("127.0.0.1"));
    for (String s :
        new String[] {
          "8.8.8.8",
          "172.32.0.1",
          "localhost",
          "192.168.1.256",
          "010.1.2.3",
          "::1",
          "192.168.1.2.evil"
        }) assertFalse(s, Protocol.lan(s));
  }

  @Test
  public void namesPreserved() throws Exception {
    for (String s : new String[] {"测试 文件.pdf", "résumé.txt", "照片🎈.jpg"})
      assertEquals(s, Protocol.name(s));
  }

  @Test
  public void unsafeNamesRejected() {
    for (String s :
        new String[] {"../secret", "CON.txt", "x.", "x ", "a\\b", "a\nb", "e\u0301.txt"})
      assertThrows(s, IOException.class, () -> Protocol.name(s));
  }

  @Test
  public void messageBoundary() throws Exception {
    assertEquals("你好\nhello", Protocol.text("你好\nhello"));
    Protocol.text("x".repeat(4096));
    assertThrows(IOException.class, () -> Protocol.text("中".repeat(1366)));
    assertThrows(IOException.class, () -> Protocol.text("\t "));
    assertThrows(IOException.class, () -> Protocol.text("a\0b"));
  }

  @Test
  public void fileLimitsAndDuplicate() throws Exception {
    JSONArray f =
        new JSONArray()
            .put(
                new JSONObject()
                    .put("name", "a.txt")
                    .put("size", Protocol.MAX_FILE)
                    .put("sha256", "a".repeat(64)));
    Protocol.files(f);
    f.getJSONObject(0).put("size", Protocol.MAX_FILE + 1);
    assertThrows(IOException.class, () -> Protocol.files(f));
    f.getJSONObject(0).put("size", 1);
    f.put(new JSONObject().put("name", "A.txt").put("size", 1).put("sha256", "b".repeat(64)));
    assertThrows(IOException.class, () -> Protocol.files(f));
  }

  @Test
  public void headerSmugglingRejected() {
    for (String s :
        new String[] {
          "POST / HTTP/1.1\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n",
          "POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n",
          "GET / HTTP/1.1\n\n"
        })
      assertThrows(
          IOException.class,
          () -> Wire.head(new ByteArrayInputStream(s.getBytes(StandardCharsets.US_ASCII))));
  }

  @Test
  public void bodyLimitsAndTruncation() {
    assertThrows(
        IOException.class, () -> Wire.body(new ByteArrayInputStream(new byte[0]), 32769, 32768));
    assertThrows(EOFException.class, () -> Wire.body(new ByteArrayInputStream(new byte[1]), 2, 10));
  }

  @Test
  public void comparisonCodeVector() throws Exception {
    assertEquals(
        "5724-771D-4988-37C7",
        Protocol.code("a".repeat(64), "b".repeat(64), "c".repeat(64), "d".repeat(32)));
  }
}
