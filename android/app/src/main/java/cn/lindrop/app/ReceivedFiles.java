package cn.lindrop.app;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;

/** Commit an already hash-verified file within the app's private received directory. */
public final class ReceivedFiles {
  private ReceivedFiles() {}

  public static synchronized void commit(File temp, File target, boolean overwrite)
      throws IOException {
    if (!temp.getCanonicalFile().getParentFile().equals(target.getCanonicalFile().getParentFile()))
      throw new IOException("接收文件必须在同一目录提交");
    if (target.exists() && !overwrite) throw new IOException("出现同名文件，请重新确认");
    if (overwrite) {
      // Preserve the old file if the filesystem cannot do atomic replacement.
      Files.move(
          temp.toPath(),
          target.toPath(),
          StandardCopyOption.ATOMIC_MOVE,
          StandardCopyOption.REPLACE_EXISTING);
    } else {
      // Hard links are unavailable in some Android compatibility filesystems.
      // No REPLACE_EXISTING: a late collision must fail, not overwrite a file.
      // All receive commits share this lock; this private directory is not
      // writable by another app and exports never write into it.
      Files.move(temp.toPath(), target.toPath());
    }
  }
}
