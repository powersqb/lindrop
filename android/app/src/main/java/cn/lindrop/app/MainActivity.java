package cn.lindrop.app;

import android.app.*;
import android.content.*;
import android.database.Cursor;
import android.graphics.Color;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.*;
import android.provider.*;
import android.view.*;
import android.widget.*;
import java.io.*;
import java.util.*;
import java.util.concurrent.*;

/** Compact native UI. Every network/hash/copy operation runs outside the UI thread. */
public final class MainActivity extends Activity implements Node.Ui {
  static final int PICK = 1, EXPORT = 2;
  static final int GREEN = 0xff168a65;
  final ExecutorService background = Executors.newSingleThreadExecutor();
  final Map<String, Deque<String>> history = new HashMap<>();
  final Deque<Runnable> dialogs = new ArrayDeque<>();
  boolean dialogVisible;
  final java.util.concurrent.atomic.AtomicBoolean preparingCancel =
      new java.util.concurrent.atomic.AtomicBoolean();
  volatile Node node;
  volatile boolean destroyed;
  WifiManager.MulticastLock multicastLock;
  Spinner devices;
  ArrayAdapter<String> adapter;
  List<Protocol.Peer> peers = new ArrayList<>();
  String selectedId;
  TextView transcript, status;
  EditText input;
  ScrollView scroll;
  Button send, files;
  File exporting;
  Protocol.Peer sendingPeer;
  android.content.SharedPreferences prefs;

  @Override
  public void onCreate(Bundle state) {
    super.onCreate(state);
    prefs = getSharedPreferences("identity", MODE_PRIVATE);
    build();
    startNode();
  }

  int dp(int v) {
    return Math.round(v * getResources().getDisplayMetrics().density);
  }

  Button button(String label) {
    Button b = new Button(this);
    b.setText(label);
    b.setTextSize(13);
    b.setAllCaps(false);
    return b;
  }

  LinearLayout row() {
    LinearLayout l = new LinearLayout(this);
    l.setOrientation(LinearLayout.HORIZONTAL);
    l.setGravity(Gravity.CENTER_VERTICAL);
    return l;
  }

  void build() {
    getWindow().setStatusBarColor(GREEN);
    getWindow().setNavigationBarColor(Color.WHITE);
    LinearLayout root = new LinearLayout(this);
    root.setOrientation(LinearLayout.VERTICAL);
    root.setPadding(dp(12), dp(10), dp(12), dp(8));
    root.setBackgroundColor(0xfff6f8f7);
    setContentView(root);
    // Target 35 draws edge-to-edge on Android 15: respect bars and keyboard insets.
    root.setOnApplyWindowInsetsListener(
        (v, insets) -> {
          if (Build.VERSION.SDK_INT >= 30) {
            android.graphics.Insets bars =
                insets.getInsets(WindowInsets.Type.systemBars() | WindowInsets.Type.ime());
            root.setPadding(
                dp(12) + bars.left, dp(10) + bars.top, dp(12) + bars.right, dp(8) + bars.bottom);
          } else
            root.setPadding(
                dp(12) + insets.getSystemWindowInsetLeft(),
                dp(10) + insets.getSystemWindowInsetTop(),
                dp(12) + insets.getSystemWindowInsetRight(),
                dp(8) + insets.getSystemWindowInsetBottom());
          return insets;
        });
    LinearLayout title = row();
    TextView heading = new TextView(this);
    heading.setText("邻传");
    heading.setTextColor(GREEN);
    heading.setTextSize(24);
    heading.setTypeface(null, android.graphics.Typeface.BOLD);
    title.addView(heading, new LinearLayout.LayoutParams(0, dp(44), 1));
    Button settings = button("设置");
    title.addView(settings);
    settings.setOnClickListener(v -> settings());
    root.addView(title);
    status = new TextView(this);
    status.setText("启动中…");
    status.setTextSize(12);
    status.setTextColor(0xff58665f);
    status.setMaxLines(3);
    root.addView(status);
    devices = new Spinner(this);
    adapter =
        new ArrayAdapter<>(
            this,
            android.R.layout.simple_spinner_dropdown_item,
            new ArrayList<>(Collections.singletonList("等待局域网设备…")));
    devices.setAdapter(adapter);
    root.addView(devices, new LinearLayout.LayoutParams(-1, dp(48)));
    devices.setOnItemSelectedListener(
        new android.widget.AdapterView.OnItemSelectedListener() {
          public void onNothingSelected(android.widget.AdapterView<?> p) {}

          public void onItemSelected(android.widget.AdapterView<?> p, View v, int pos, long id) {
            if (pos < peers.size()) {
              selectedId = peers.get(pos).id;
              render();
            }
          }
        });
    scroll = new ScrollView(this);
    scroll.setFillViewport(true);
    transcript = new TextView(this);
    transcript.setTextSize(15);
    transcript.setTextColor(0xff243b30);
    transcript.setTextIsSelectable(true);
    transcript.setPadding(dp(12), dp(12), dp(12), dp(12));
    transcript.setBackgroundColor(Color.WHITE);
    scroll.addView(transcript);
    root.addView(scroll, new LinearLayout.LayoutParams(-1, 0, 1));
    LinearLayout compose = row();
    input = new EditText(this);
    input.setHint("输入消息");
    input.setTextSize(15);
    input.setInputType(
        android.text.InputType.TYPE_CLASS_TEXT | android.text.InputType.TYPE_TEXT_FLAG_MULTI_LINE);
    input.setMaxLines(4);
    compose.addView(input, new LinearLayout.LayoutParams(0, -2, 1));
    send = button("发送");
    compose.addView(send);
    send.setOnClickListener(v -> send());
    root.addView(compose);
    input.setOnKeyListener(
        (v, key, event) -> {
          if (key == KeyEvent.KEYCODE_ENTER
              && event.getAction() == KeyEvent.ACTION_DOWN
              && !event.isShiftPressed()) {
            send();
            return true;
          }
          return false;
        });
    LinearLayout tools = row();
    files = button("发文件");
    tools.addView(files, new LinearLayout.LayoutParams(0, -2, 1));
    files.setOnClickListener(v -> pick());
    Button received = button("接收文件");
    tools.addView(received, new LinearLayout.LayoutParams(0, -2, 1));
    received.setOnClickListener(v -> received());
    Button cancel = button("取消传输");
    tools.addView(cancel, new LinearLayout.LayoutParams(0, -2, 1));
    cancel.setOnClickListener(
        v -> {
          preparingCancel.set(true);
          if (node != null) node.cancel();
        });
    root.addView(tools);
    render();
  }

  void startNode() {
    background.execute(
        () -> {
          try {
            Identity identity = new Identity();
            String id = prefs.getString("device-id", null);
            if (id == null) {
              id = Protocol.random(16);
              if (!prefs.edit().putString("device-id", id).commit())
                throw new IOException("无法保存设备身份");
            }
            String deviceName = prefs.getString("device-name", Build.MODEL);
            if (deviceName.length() > 40) deviceName = deviceName.substring(0, 40);
            WifiManager wifi = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (wifi != null) {
              multicastLock = wifi.createMulticastLock("lindrop-discovery");
              multicastLock.setReferenceCounted(false);
              multicastLock.acquire();
            }
            Node.Trust trust =
                new Node.Trust() {
                  public String get(String peerId) {
                    return prefs.getString("pin-" + peerId, null);
                  }

                  public synchronized void remember(String peerId, String fp) throws Exception {
                    long count =
                        prefs.getAll().keySet().stream().filter(k -> k.startsWith("pin-")).count();
                    if (get(peerId) == null && count >= 128)
                      throw new IOException("记住的设备已达 128 台，请移除不用的设备");
                    if (!prefs.edit().putString("pin-" + peerId, fp).commit())
                      throw new IOException("无法保存设备信任");
                  }
                };
            Node created =
                new Node(
                    identity.server,
                    identity.client,
                    id,
                    identity.fingerprint,
                    Node.localHost(),
                    deviceName,
                    new File(getFilesDir(), "received"),
                    trust,
                    this);
            if (destroyed) {
              created.close();
              return;
            }
            node = created;
            created.start();
          } catch (Exception e) {
            status("启动失败：" + e.getMessage() + "。连接 Wi-Fi 后可在设置中重试。");
            if (node != null) {
              node.close();
              node = null;
            }
            if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
          }
        });
  }

  Protocol.Peer selected() {
    for (Protocol.Peer p : peers) if (p.id.equals(selectedId)) return p;
    return null;
  }

  void send() {
    Protocol.Peer p = selected();
    String text = input.getText().toString();
    if (node == null || p == null) {
      status("先选择在线设备");
      return;
    }
    try {
      Protocol.text(text);
    } catch (Exception e) {
      status(e.getMessage());
      return;
    }
    node.sendChat(p, text);
    input.setText("");
  }

  @Override
  public void peers(List<Protocol.Peer> list) {
    runOnUiThread(
        () -> {
          if (destroyed) return;
          list.sort(Comparator.comparing(p -> p.name));
          if (list.size() == peers.size()) {
            boolean same = true;
            for (int i = 0; i < list.size(); i++)
              if (!list.get(i).toString().equals(peers.get(i).toString())
                  || !list.get(i).id.equals(peers.get(i).id)
                  || !list.get(i).fp.equals(peers.get(i).fp)) same = false;
            if (same) {
              peers = list;
              return;
            }
          }
          peers = list;
          adapter.clear();
          if (list.isEmpty()) adapter.add("等待设备…或在设置中手动连接");
          else for (Protocol.Peer p : list) adapter.add(p.toString());
          int index = -1;
          for (int i = 0; i < list.size(); i++) if (list.get(i).id.equals(selectedId)) index = i;
          if (index >= 0) devices.setSelection(index);
          else if (!list.isEmpty()) {
            selectedId = list.get(0).id;
            devices.setSelection(0);
          }
          adapter.notifyDataSetChanged();
          render();
        });
  }

  @Override
  public void message(Protocol.Peer p, String text, boolean mine) {
    runOnUiThread(
        () -> {
          if (destroyed) return;
          Deque<String> rows = history.computeIfAbsent(p.id, k -> new ArrayDeque<>());
          rows.add((mine ? "我" : p.name) + "\n" + text + "\n");
          while (rows.size() > 200) rows.removeFirst();
          if (selectedId == null) selectedId = p.id;
          if (p.id.equals(selectedId)) render();
        });
  }

  void render() {
    Deque<String> rows = history.get(selectedId);
    transcript.setText(
        rows == null || rows.isEmpty()
            ? "选择设备，直接聊天或发文件。\n\n首次连接核对一次验证码。\n保持 App 打开以便收发。"
            : String.join("\n", rows));
    scroll.post(() -> scroll.fullScroll(View.FOCUS_DOWN));
  }

  @Override
  public void status(String text) {
    runOnUiThread(
        () -> {
          if (!destroyed) status.setText(text);
        });
  }

  void showNext() {
    if (dialogVisible || dialogs.isEmpty() || destroyed) return;
    dialogVisible = true;
    dialogs.removeFirst().run();
  }

  @Override
  public boolean confirm(String title, String text) throws Exception {
    CountDownLatch ready = new CountDownLatch(1);
    boolean[] result = {false};
    long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(120);
    runOnUiThread(
        () -> {
          if (destroyed) {
            ready.countDown();
            return;
          }
          dialogs.add(
              () -> {
                if (System.nanoTime() > deadline) {
                  dialogVisible = false;
                  ready.countDown();
                  showNext();
                  return;
                }
                AlertDialog d =
                    new AlertDialog.Builder(this)
                        .setTitle(title)
                        .setMessage(text)
                        .setPositiveButton("确认", (a, b) -> result[0] = true)
                        .setNegativeButton("取消", null)
                        .create();
                d.setOnDismissListener(
                    a -> {
                      ready.countDown();
                      dialogVisible = false;
                      showNext();
                    });
                d.show();
                new Handler(Looper.getMainLooper())
                    .postDelayed(
                        () -> {
                          if (d.isShowing()) {
                            result[0] = false;
                            d.dismiss();
                          }
                        },
                        Math.max(1, TimeUnit.NANOSECONDS.toMillis(deadline - System.nanoTime())));
              });
          showNext();
        });
    if (!ready.await(121, TimeUnit.SECONDS) || destroyed) return false;
    return result[0];
  }

  void pick() {
    sendingPeer = selected();
    if (node == null || sendingPeer == null) {
      status("先选择在线设备");
      return;
    }
    Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
    i.addCategory(Intent.CATEGORY_OPENABLE);
    i.setType("*/*");
    i.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
    startActivityForResult(i, PICK);
  }

  @Override
  protected void onActivityResult(int request, int result, Intent data) {
    super.onActivityResult(request, result, data);
    if (result != RESULT_OK || data == null) return;
    if (request == PICK) {
      List<Uri> uris = new ArrayList<>();
      if (data.getClipData() != null)
        for (int i = 0; i < data.getClipData().getItemCount(); i++)
          uris.add(data.getClipData().getItemAt(i).getUri());
      else if (data.getData() != null) uris.add(data.getData());
      Protocol.Peer peer = sendingPeer;
      Node current = node;
      preparingCancel.set(false);
      background.execute(
          () -> {
            List<Node.SendFile> list = new ArrayList<>();
            File pending = null;
            try {
              if (current == null || peer == null || uris.size() < 1 || uris.size() > 50)
                throw new IOException("每批最多 50 个文件");
              long batch = 0;
              status("准备文件，正在校验…");
              for (Uri u : uris) {
                String name = null;
                try (Cursor c =
                    getContentResolver()
                        .query(u, new String[] {OpenableColumns.DISPLAY_NAME}, null, null, null)) {
                  if (c != null && c.moveToFirst()) name = c.getString(0);
                }
                Protocol.name(name);
                pending = File.createTempFile("sending-", ".tmp", getCacheDir());
                long count = 0;
                try (InputStream in = getContentResolver().openInputStream(u);
                    OutputStream out = new FileOutputStream(pending)) {
                  if (in == null) throw new IOException("无法读取文件");
                  byte[] b = new byte[65536];
                  int n;
                  while ((n = in.read(b)) != -1) {
                    if (destroyed || preparingCancel.get()) throw new IOException("文件准备已取消");
                    count += n;
                    batch += n;
                    if (count > Protocol.MAX_FILE
                        || batch > 20L * 1024 * 1024 * 1024
                        || getCacheDir().getUsableSpace() < 128L * 1024 * 1024)
                      throw new IOException("文件过大或缓存空间不足");
                    out.write(b, 0, n);
                  }
                }
                list.add(new Node.SendFile(pending, name));
                pending = null;
              }
              if (preparingCancel.get()) throw new IOException("文件准备已取消");
              current.sendFiles(peer, list);
            } catch (Exception e) {
              if (pending != null) pending.delete();
              for (Node.SendFile f : list) f.file.delete();
              status("文件准备失败：" + e.getMessage());
            }
          });
    }
    if (request == EXPORT && exporting != null && data.getData() != null) {
      File file = exporting;
      Uri uri = data.getData();
      background.execute(
          () -> {
            try {
              String actual = null;
              try (Cursor c =
                  getContentResolver()
                      .query(uri, new String[] {OpenableColumns.DISPLAY_NAME}, null, null, null)) {
                if (c != null && c.moveToFirst()) actual = c.getString(0);
              }
              if (!file.getName().equals(actual))
                throw new IOException("系统保存位置改了文件名，请重新选择并保留原名；未写入文件");
              try (InputStream in = new FileInputStream(file);
                  OutputStream out = getContentResolver().openOutputStream(uri, "wt")) {
                if (out == null) throw new IOException("无法打开保存位置");
                byte[] b = new byte[65536];
                int n;
                while ((n = in.read(b)) != -1) out.write(b, 0, n);
              }
              status("已导出：" + file.getName());
            } catch (Exception e) {
              status("导出失败：" + e.getMessage());
            }
          });
    }
  }

  void received() {
    File dir = new File(getFilesDir(), "received");
    File[] list = dir.listFiles(f -> f.isFile() && !f.getName().startsWith(".receiving-"));
    if (list == null || list.length == 0) {
      status("还没有接收文件");
      return;
    }
    Arrays.sort(list, Comparator.comparingLong(File::lastModified).reversed());
    String[] names = Arrays.stream(list).map(File::getName).toArray(String[]::new);
    new AlertDialog.Builder(this)
        .setTitle("接收文件 · 点击导出原文件")
        .setItems(
            names,
            (d, i) -> {
              exporting = list[i];
              Intent pick = new Intent(Intent.ACTION_CREATE_DOCUMENT);
              pick.addCategory(Intent.CATEGORY_OPENABLE);
              pick.setType("application/octet-stream");
              pick.putExtra(Intent.EXTRA_TITLE, exporting.getName());
              startActivityForResult(pick, EXPORT);
            })
        .setNegativeButton("关闭", null)
        .show();
  }

  void settings() {
    String local = node == null ? "未连接" : node.host + ":" + node.port;
    new AlertDialog.Builder(this)
        .setTitle("邻传安卓 0.1.0 · 测试版")
        .setItems(
            new String[] {"手动连接电脑", "重试网络连接", "修改设备名", "移除设备信任", "关于与使用说明"},
            (d, i) -> {
              if (i == 0) manual();
              if (i == 1) {
                if (node != null) node.close();
                node = null;
                if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
                startNode();
              }
              if (i == 2) rename();
              if (i == 3) forget();
              if (i == 4)
                new AlertDialog.Builder(this)
                    .setTitle("邻传安卓 0.1.0")
                    .setMessage(
                        "本机："
                            + local
                            + "\n"
                            + "仅局域网直连；无后台公网访问。\n"
                            + "保持 App 打开，首次核对验证码。\n"
                            + "设备证书保存在 Android Keystore。\n"
                            + "接收文件可点击导出，保留原名和内容。")
                    .setPositiveButton("知道了", null)
                    .show();
            })
        .setNegativeButton("关闭", null)
        .show();
  }

  void manual() {
    EditText value = new EditText(this);
    value.setSingleLine();
    value.setHint("例如 192.168.1.20:8765");
    new AlertDialog.Builder(this)
        .setTitle("输入电脑邻传的局域网地址")
        .setView(value)
        .setPositiveButton(
            "连接",
            (d, i) -> {
              try {
                String[] a = value.getText().toString().trim().split(":");
                if (a.length != 2 || node == null) throw new IOException("格式：局域网 IPv4:端口");
                node.manual(a[0], Integer.parseInt(a[1]));
              } catch (Exception e) {
                status(e.getMessage());
              }
            })
        .setNegativeButton("取消", null)
        .show();
  }

  void rename() {
    EditText value = new EditText(this);
    value.setText(prefs.getString("device-name", Build.MODEL));
    new AlertDialog.Builder(this)
        .setTitle("设备名 · 重试连接后生效")
        .setView(value)
        .setPositiveButton(
            "保存",
            (d, i) -> {
              String n = value.getText().toString().trim();
              if (n.isEmpty() || n.length() > 40 || n.chars().anyMatch(c -> c < 32 || c == 127)) {
                status("设备名为 1 至 40 个字符");
                return;
              }
              prefs.edit().putString("device-name", n).apply();
            })
        .setNegativeButton("取消", null)
        .show();
  }

  void forget() {
    List<String> ids = new ArrayList<>();
    for (String k : prefs.getAll().keySet()) if (k.startsWith("pin-")) ids.add(k);
    if (ids.isEmpty()) {
      status("没有记住的设备");
      return;
    }
    String[] labels =
        ids.stream()
            .map(
                k -> {
                  String id = k.substring(4);
                  for (Protocol.Peer p : peers)
                    if (p.id.equals(id)) return p.name + " · " + id.substring(0, 8);
                  return id;
                })
            .toArray(String[]::new);
    new AlertDialog.Builder(this)
        .setTitle("移除信任后需要重新核对")
        .setItems(
            labels,
            (d, i) ->
                new AlertDialog.Builder(this)
                    .setTitle("确定移除这台设备的信任？")
                    .setMessage(labels[i])
                    .setPositiveButton(
                        "移除",
                        (a, b) -> {
                          prefs.edit().remove(ids.get(i)).apply();
                          if (node != null) node.close();
                          node = null;
                          status("信任已移除，请重试网络连接");
                        })
                    .setNegativeButton("取消", null)
                    .show())
        .setNegativeButton("关闭", null)
        .show();
  }

  @Override
  protected void onDestroy() {
    destroyed = true;
    if (node != null) node.close();
    if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
    background.shutdownNow();
    super.onDestroy();
  }
}
