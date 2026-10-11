package cn.lindrop.app;

import static org.junit.Assert.*;

import java.io.*;
import java.nio.file.Files;
import org.junit.Test;

public class ReceivedFilesTest {
  @Test
  public void newFileKeepsNameAndBytesAndRejectsLateCollision() throws Exception {
    File dir = Files.createTempDirectory("received-test").toFile();
    File temp = new File(dir, ".receiving-test.part");
    File target = new File(dir, "原名.txt");
    byte[] bytes = {0, 1, 2, (byte) 255};
    try {
      Files.write(temp.toPath(), bytes);
      ReceivedFiles.commit(temp, target, false);
      assertArrayEquals(bytes, Files.readAllBytes(target.toPath()));
      assertFalse(temp.exists());
      Files.write(temp.toPath(), new byte[] {9});
      assertThrows(IOException.class, () -> ReceivedFiles.commit(temp, target, false));
      assertArrayEquals(bytes, Files.readAllBytes(target.toPath()));
      assertTrue(temp.exists());
      ReceivedFiles.commit(temp, target, true);
      assertArrayEquals(new byte[] {9}, Files.readAllBytes(target.toPath()));
    } finally {
      temp.delete();
      target.delete();
      dir.delete();
    }
  }
}
