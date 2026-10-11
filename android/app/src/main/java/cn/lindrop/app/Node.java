package cn.lindrop.app;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.security.*;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;
import javax.net.ssl.*;
import org.json.*;

/** Platform independent LAN node. Android only supplies identity, storage and UI callbacks. */
public final class Node implements AutoCloseable {
  public interface Trust {
    String get(String id);

    void remember(String id, String fp) throws Exception;

    default List<Protocol.Peer> endpoints() {
      return Collections.emptyList();
    }

    default void rememberEndpoint(Protocol.Peer peer) throws Exception {}
  }

  public interface Ui {
    default void networkError(Exception error) {}

    void peers(List<Protocol.Peer> peers);

    void message(Protocol.Peer peer, String text, boolean mine);

    void status(String text);

    boolean confirm(String title, String text) throws Exception;
  }

  final SSLContext tlsServer, tlsClient;
  final Trust trust;
  final Ui ui;
  public final String id, fp, host, name;
  public volatile int port;
  final File received;
  final Map<String, Protocol.Peer> peers = new ConcurrentHashMap<>();
  final Set<String> manualPeers = ConcurrentHashMap.newKeySet();
  final Map<String, Protocol.Peer> savedPeers = new ConcurrentHashMap<>();
  final Set<String> probing = ConcurrentHashMap.newKeySet();
  final ThreadPoolExecutor reconnect =
      new ThreadPoolExecutor(2, 2, 30, TimeUnit.SECONDS, new ArrayBlockingQueue<>(16));
  final Map<String, Channel> channels = new ConcurrentHashMap<>();
  final Map<String, Outgoing> outgoing = new ConcurrentHashMap<>();
  final Map<String, Offer> offers = new ConcurrentHashMap<>();
  final Map<String, Deque<Long>> attempts = new HashMap<>();
  final Map<String, Long> presenceAttempts = new HashMap<>();
  final Set<Socket> sockets = ConcurrentHashMap.newKeySet();
  final ExecutorService actions = Executors.newFixedThreadPool(4);
  final ThreadPoolExecutor handlers =
      new ThreadPoolExecutor(2, 12, 30, TimeUnit.SECONDS, new ArrayBlockingQueue<>(32));
  final ScheduledExecutorService timers = Executors.newScheduledThreadPool(2);
  final AtomicBoolean sending = new AtomicBoolean();
  volatile boolean closed;
  volatile Offer receiving;
  volatile Socket sendingSocket;
  final AtomicBoolean cancelSend = new AtomicBoolean();
  final Object chatConnectLock = new Object();
  SSLServerSocket listener;
  MulticastSocket multicast;

  static long now() {
    return System.nanoTime() / 1000000;
  }

  static class Channel {
    String id = Protocol.random(16), secret = Protocol.random(24), code;
    volatile String status = "pending";
    Protocol.Peer peer;
    volatile long expiry = now() + 120000;
    long seq;
    final Deque<JSONObject> messages = new ArrayDeque<>();
    final Deque<Long> rates = new ArrayDeque<>();
  }

  static class Outgoing {
    Protocol.Peer peer;
    String id, secret;
    long cursor, expiry = now() + 3600000;
    volatile boolean active;
    ScheduledFuture<?> poll;
  }

  static class Offer {
    String id = Protocol.random(16), secret = Protocol.random(24), code;
    volatile String token = "", status = "pending";
    Protocol.Peer peer;
    JSONArray files;
    volatile long expiry = now() + 120000;
    final Set<Integer> complete = new HashSet<>(), active = new HashSet<>();
    boolean overwrite;
    volatile Socket socket;
  }

  public Node(
      SSLContext server,
      SSLContext client,
      String id,
      String fp,
      String host,
      String name,
      File received,
      Trust trust,
      Ui ui)
      throws Exception {
    if (!Protocol.lan(host) || !id.matches("[0-9a-f]{32}") || !fp.matches("[0-9a-f]{64}"))
      throw new IOException("本机身份或地址无效");
    this.tlsServer = server;
    this.tlsClient = client;
    this.id = id;
    this.fp = fp;
    this.host = host;
    this.name = name;
    this.received = received;
    this.trust = trust;
    this.ui = ui;
    if (!received.isDirectory() && !received.mkdirs()) throw new IOException("无法创建接收目录");
  }

  public JSONObject identity() throws JSONException {
    return new JSONObject()
        .put("id", id)
        .put("host", host)
        .put("presence", true)
        .put("name", name)
        .put("port", port)
        .put("fingerprint", fp);
  }

  public static String localHost() throws Exception {
    List<NetworkInterface> list = Collections.list(NetworkInterface.getNetworkInterfaces());
    list.sort(Comparator.comparingInt(n -> n.getName().startsWith("wlan") ? 0 : 1));
    for (NetworkInterface n : list)
      if (n.isUp() && !n.isLoopback())
        for (InetAddress a : Collections.list(n.getInetAddresses()))
          if (a instanceof Inet4Address
              && Protocol.lan(a.getHostAddress())
              && !a.isLoopbackAddress()) return a.getHostAddress();
    throw new IOException("未找到局域网 IPv4，请连接 Wi-Fi 后重试");
  }

  public void start() throws Exception {
    listener = (SSLServerSocket) tlsServer.getServerSocketFactory().createServerSocket();
    listener.setReuseAddress(true);
    try {
      listener.bind(new InetSocketAddress(host, 8765));
    } catch (BindException e) {
      listener.bind(new InetSocketAddress(host, 0));
    }
    port = listener.getLocalPort();
    listener.setEnabledProtocols(new String[] {"TLSv1.2"});
    Thread accept =
        new Thread(
            () -> {
              while (!closed)
                try {
                  SSLSocket s = (SSLSocket) listener.accept();
                  if (!Protocol.lan(s.getInetAddress().getHostAddress())) {
                    s.close();
                    continue;
                  }
                  s.setSoTimeout(30000);
                  sockets.add(s);
                  try {
                    handlers.execute(() -> serve(s));
                  } catch (RejectedExecutionException e) {
                    sockets.remove(s);
                    s.close();
                  }
                } catch (IOException e) {
                  if (!closed) ui.status("监听失败：" + e.getMessage());
                }
            },
            "lindrop-tls");
    accept.setDaemon(true);
    accept.start();
    try {
      multicast = new MulticastSocket(null);
      multicast.setReuseAddress(true);
      multicast.bind(new InetSocketAddress(Protocol.DISCOVERY_PORT));
      multicast.setTimeToLive(1);
      multicast.setBroadcast(true);
      multicast.setSoTimeout(1000);
      NetworkInterface network = NetworkInterface.getByInetAddress(InetAddress.getByName(host));
      multicast.setNetworkInterface(network);
      multicast.joinGroup(new InetSocketAddress(Protocol.GROUP, Protocol.DISCOVERY_PORT), network);
      Thread discover =
          new Thread(
              () -> {
                while (!closed)
                  try {
                    byte[] b = new byte[1537];
                    DatagramPacket p = new DatagramPacket(b, b.length);
                    multicast.receive(p);
                    if (p.getLength() > 1536) continue;
                    JSONObject j =
                        new JSONObject(
                            new String(p.getData(), 0, p.getLength(), StandardCharsets.UTF_8));
                    if (!j.optString("app").equals("lindrop") || j.optInt("protocol") != 2)
                      continue;
                    Protocol.Peer peer = new Protocol.Peer(j, p.getAddress().getHostAddress());
                    if (!peer.id.equals(id) && (peers.size() < 128 || peers.containsKey(peer.id)))
                      peers.put(peer.id, peer);
                  } catch (SocketTimeoutException ignored) {
                  } catch (Exception ignored) {
                    /* Untrusted discovery packets are discarded. */
                  }
              },
              "lindrop-discovery");
      discover.setDaemon(true);
      discover.start();
      timers.scheduleWithFixedDelay(
          () -> {
            try {
              byte[] b =
                  identity()
                      .put("app", "lindrop")
                      .put("protocol", 2)
                      .toString()
                      .getBytes(StandardCharsets.UTF_8);
              multicast.send(
                  new DatagramPacket(
                      b, b.length, InetAddress.getByName(Protocol.GROUP), Protocol.DISCOVERY_PORT));
              multicast.send(
                  new DatagramPacket(
                      b,
                      b.length,
                      InetAddress.getByName("255.255.255.255"),
                      Protocol.DISCOVERY_PORT));
            } catch (Exception e) {
              if (!closed) ui.status("发现受限，可尝试手动连接");
            }
          },
          0,
          2,
          TimeUnit.SECONDS);
    } catch (Exception e) {
      if (multicast != null) multicast.close();
      ui.status("组播发现不可用，可手动连接：" + e.getMessage());
    }
    timers.scheduleWithFixedDelay(
        () -> {
          long t = now();
          peers.values().removeIf(p -> t - p.seen > 8000 && !manualPeers.contains(p.id));
          channels.values().removeIf(c -> c.expiry < t);
          offers
              .values()
              .removeIf(
                  o -> {
                    synchronized (o) {
                      return o.expiry < t && o.active.isEmpty();
                    }
                  });
          ui.peers(new ArrayList<>(peers.values()));
        },
        0,
        1,
        TimeUnit.SECONDS);
    startSaved();
    ui.status("在线 · " + host + ":" + port + " · 安卓 0.1.3");
  }

  void startSaved() {
    for (Protocol.Peer p : trust.endpoints()) {
      if (savedPeers.size() >= 16) break;
      if (!p.id.equals(id) && Protocol.equal(trust.get(p.id), p.fp)) savedPeers.put(p.id, p);
    }
    timers.scheduleWithFixedDelay(this::probeSaved, 0, 5, TimeUnit.SECONDS);
  }

  void track(Protocol.Peer p) throws Exception {
    if (!remembered(p) || p.id.equals(id)) return;
    if (savedPeers.containsKey(p.id) || savedPeers.size() < 16) {
      trust.rememberEndpoint(p);
      savedPeers.put(p.id, p);
    }
  }

  void probeSaved() {
    for (Protocol.Peer p : savedPeers.values()) {
      if (closed || !Protocol.equal(trust.get(p.id), p.fp) || !probing.add(p.id)) continue;
      try {
        reconnect.execute(
            () -> {
              try {
                Protocol.Peer online = KnownPeers.probe(tlsClient, p, host);
                if (!closed && Protocol.equal(trust.get(p.id), p.fp)) {
                  peers.put(p.id, online);
                  ui.peers(new ArrayList<>(peers.values()));
                }
              } catch (Exception e) {
                peers.computeIfPresent(
                    p.id,
                    (k, current) ->
                        current.host.equals(p.host)
                                && current.port == p.port
                                && current.fp.equals(p.fp)
                            ? null
                            : current);
                if (!closed) ui.peers(new ArrayList<>(peers.values()));
              } finally {
                probing.remove(p.id);
              }
            });
      } catch (RejectedExecutionException e) {
        probing.remove(p.id);
      }
    }
  }

  boolean remembered(Protocol.Peer p) throws IOException {
    String old = trust.get(p.id);
    if (old != null && !Protocol.equal(old, p.fp))
      throw new IOException("设备证书已变化，已阻止连接；请核实后在设置中移除旧信任");
    return old != null;
  }

  void remember(Protocol.Peer p) throws Exception {
    remembered(p);
    trust.remember(p.id, p.fp);
    track(p);
  }

  JSONObject request(Protocol.Peer p, String method, String path, JSONObject data, String secret)
      throws Exception {
    remembered(p);
    return Wire.request(tlsClient, p, host, method, path, data, secret);
  }

  Protocol.Peer reverse(JSONObject j, String ip, boolean chat) throws Exception {
    Protocol.Peer p = new Protocol.Peer(j, ip);
    remembered(p);
    JSONObject hello = request(p, "GET", "/peer/hello", null, null);
    if (!hello.optString("id").equals(p.id)
        || !Protocol.equal(hello.optString("fingerprint"), p.fp)
        || hello.optInt("port") != p.port
        || hello.optInt("protocol") != 2
        || (chat && !hello.optBoolean("chat"))) throw new IOException("设备身份或协议不一致");
    return new Protocol.Peer(hello, ip);
  }

  synchronized void rate(String ip) throws IOException {
    long t = now();
    attempts.entrySet().removeIf(e -> e.getValue().isEmpty() || t - e.getValue().getLast() > 60000);
    Deque<Long> q = attempts.computeIfAbsent(ip, k -> new ArrayDeque<>());
    while (!q.isEmpty() && t - q.getFirst() > 60000) q.removeFirst();
    if (q.size() >= 5 || attempts.size() > 128) throw new IOException("请求过于频繁");
    q.add(t);
  }

  void serve(SSLSocket socket) {
    try (SSLSocket s = socket) {
      s.startHandshake();
      InputStream in = new BufferedInputStream(s.getInputStream());
      OutputStream out = s.getOutputStream();
      try {
        Wire.Head h = Wire.head(in);
        String[] p = h.first.split(" ");
        if (p.length != 3 || !p[2].equals("HTTP/1.1")) throw new IOException("HTTP 请求无效");
        String method = p[0], path = p[1], ip = s.getInetAddress().getHostAddress();
        String authority = host + ":" + port;
        if (!authority.equals(h.headers.get("host"))
            || !("https://" + authority).equals(h.headers.get("origin")))
          throw new IOException("请求来源无效");
        if (method.equals("POST") && path.startsWith("/peer/upload/")) {
          upload(s, in, out, h, path, ip);
          return;
        }
        JSONObject data = new JSONObject();
        if (method.equals("POST"))
          data =
              new JSONObject(new String(Wire.body(in, h.length(), 32768), StandardCharsets.UTF_8));
        else if (!method.equals("GET")) throw new IOException("方法无效");
        JSONObject result = route(method, path, data, h.headers.get("x-offer-secret"), ip);
        Wire.respond(out, path.endsWith("/offer") ? 201 : 200, result);
      } catch (Exception e) {
        Wire.respond(
            out,
            400,
            new JSONObject().put("error", e.getMessage() == null ? "请求失败" : e.getMessage()));
      }
    } catch (Exception error) {
      ui.networkError(error);
    } finally {
      sockets.remove(socket);
    }
  }

  static void nonce(JSONObject j) throws Exception {
    if (!j.optString("nonce").matches("[0-9a-f]{64}")) throw new IOException("随机数无效");
  }

  Channel channel(String id, String secret, String ip) throws IOException {
    Channel c = channels.get(id);
    if (c == null
        || c.expiry < now()
        || !c.peer.host.equals(ip)
        || !Protocol.equal(c.secret, secret)) throw new IOException("聊天未授权或已过期");
    return c;
  }

  Offer offer(String id, String secret, String ip) throws IOException {
    Offer o = offers.get(id);
    if (o == null
        || o.expiry < now()
        || !o.peer.host.equals(ip)
        || !Protocol.equal(o.secret, secret)) throw new IOException("文件请求未授权或已过期");
    return o;
  }

  JSONObject route(String method, String path, JSONObject data, String secret, String ip)
      throws Exception {
    if (method.equals("GET") && path.equals("/peer/hello"))
      return identity()
          .put("app", "lindrop")
          .put("protocol", 2)
          .put("chat", true)
          .put("remembered_chat", true);
    if (method.equals("POST") && path.equals("/peer/presence")) {
      synchronized (presenceAttempts) {
        long t = now();
        presenceAttempts.entrySet().removeIf(e -> t - e.getValue() > 60000);
        if (t - presenceAttempts.getOrDefault(ip, -60000L) < 2000
            || (!presenceAttempts.containsKey(ip) && presenceAttempts.size() >= 128))
          throw new IOException("设备发现请求过于频繁");
        presenceAttempts.put(ip, t);
      }
      if (!data.optString("app").equals("lindrop") || data.optInt("protocol") != 2)
        throw new IOException("设备发现协议无效");
      Protocol.Peer peer = reverse(data, ip, false);
      if (peers.size() >= 128 && !peers.containsKey(peer.id)) throw new IOException("设备数量已达上限");
      if (!peer.id.equals(id)) {
        peers.put(peer.id, peer);
        if (remembered(peer)) track(peer);
        ui.peers(new ArrayList<>(peers.values()));
      }
      return new JSONObject().put("id", id).put("discovered", true);
    }
    if (method.equals("POST") && path.equals("/peer/chat/offer")) {
      rate(ip);
      nonce(data);
      if (channels.size() >= 16
          || channels.values().stream().anyMatch(c -> c.status.equals("pending")))
        throw new IOException("正在确认其他连接");
      Protocol.Peer peer = reverse(data, ip, true);
      Channel c = new Channel();
      c.peer = peer;
      c.code = Protocol.code(peer.fp, fp, data.getString("nonce"), c.id);
      synchronized (channels) {
        if (channels.size() >= 16
            || channels.values().stream().anyMatch(v -> v.status.equals("pending")))
          throw new IOException("正在确认其他连接");
        channels.put(c.id, c);
      }
      if (data.optBoolean("automatic") && remembered(peer)) {
        c.status = "accepted";
        c.expiry = now() + 3600000;
      } else
        actions.execute(
            () -> {
              try {
                boolean yes =
                    ui.confirm(
                        "核对设备 · " + peer.name,
                        "请确认双方显示相同验证码：\n\n" + c.code + "\n\n确认后记住设备，以后直接聊天。");
                synchronized (c) {
                  if (!c.status.equals("pending") || c.expiry < now()) return;
                  if (yes) {
                    remember(peer);
                    c.status = "accepted";
                    c.expiry = now() + 3600000;
                  } else {
                    c.status = "rejected";
                    c.expiry = now() + 10000;
                  }
                }
              } catch (Exception e) {
                c.status = "rejected";
                ui.status(e.getMessage());
              }
            });
      return new JSONObject()
          .put("id", c.id)
          .put("secret", c.secret)
          .put("code", c.code)
          .put("receiver_id", id);
    }
    String[] p = path.split("/");
    if (p.length == 5 && p[1].equals("peer") && p[2].equals("chat")) {
      Channel c = channel(p[3], secret, ip);
      synchronized (c) {
        if (method.equals("GET") && p[4].matches("[0-9]{1,12}")) {
          long cursor = Long.parseLong(p[4]);
          JSONArray msgs = new JSONArray();
          if (c.status.equals("active"))
            for (JSONObject m : c.messages)
              if (m.getLong("seq") > cursor && msgs.length() < 4) msgs.put(m);
          return new JSONObject()
              .put("status", c.status)
              .put("messages", msgs)
              .put("oldest", c.messages.isEmpty() ? 0 : c.messages.getFirst().getLong("seq"));
        }
        if (method.equals("POST") && p[4].equals("activate")) {
          if (!c.status.equals("accepted")) throw new IOException("连接尚未确认");
          c.status = "active";
          ui.status("已连接：" + c.peer.name);
          return new JSONObject();
        }
        if (method.equals("POST") && p[4].equals("send")) {
          JSONObject m =
              append(c, data.getString("text"), data.getString("message_id"), "initiator");
          return new JSONObject().put("id", m.getString("id"));
        }
        if (method.equals("POST") && p[4].equals("close")) {
          c.status = "closed";
          c.expiry = now() + 10000;
          return new JSONObject();
        }
      }
    }
    if (method.equals("POST") && path.equals("/peer/offer")) {
      rate(ip);
      nonce(data);
      JSONArray files = data.getJSONArray("files");
      Protocol.files(files);
      Protocol.Peer peer = reverse(data, ip, false);
      Offer o = new Offer();
      o.peer = peer;
      o.files = files;
      o.code = Protocol.code(peer.fp, fp, data.getString("nonce"), o.id);
      synchronized (offers) {
        if (offers.size() >= 16
            || offers.values().stream()
                .anyMatch(v -> v.status.equals("pending") || v.status.equals("accepted")))
          throw new IOException("正在接收其他文件");
        offers.put(o.id, o);
        receiving = o;
      }
      actions.execute(() -> decide(o));
      return new JSONObject()
          .put("id", o.id)
          .put("secret", o.secret)
          .put("code", o.code)
          .put("receiver_id", id);
    }
    if (p.length == 4 && p[1].equals("peer")) {
      if (method.equals("GET") && p[2].equals("offers")) {
        Offer o = offer(p[3], secret, ip);
        synchronized (o) {
          JSONArray complete = new JSONArray();
          for (int i : o.complete) complete.put(i);
          return new JSONObject()
              .put("status", o.status)
              .put("token", o.token)
              .put("complete", complete);
        }
      }
      if (method.equals("POST") && p[2].equals("cancel")) {
        Offer o = offer(p[3], secret, ip);
        abort(o);
        return new JSONObject();
      }
    }
    throw new IOException("接口不存在");
  }

  JSONObject append(Channel c, String text, String mid, String author) throws Exception {
    Protocol.text(text);
    if (!mid.matches("[0-9a-f]{32}") || !c.status.equals("active") || c.expiry < now())
      throw new IOException("聊天未就绪");
    for (JSONObject m : c.messages)
      if (m.getString("id").equals(mid)) {
        if (!m.getString("text").equals(text) || !m.getString("author").equals(author))
          throw new IOException("消息标识重复");
        return m;
      }
    long t = now();
    while (!c.rates.isEmpty() && t - c.rates.getFirst() > 60000) c.rates.removeFirst();
    if (c.rates.size() >= 120) throw new IOException("消息发送过快");
    c.rates.add(t);
    JSONObject m =
        new JSONObject().put("seq", ++c.seq).put("id", mid).put("text", text).put("author", author);
    c.messages.add(m);
    while (c.messages.size() > 200) c.messages.removeFirst();
    ui.message(c.peer, text, author.equals("receiver"));
    return m;
  }

  void decide(Offer o) {
    try {
      StringBuilder desc = new StringBuilder(o.peer.name + " 发来：\n");
      long total = 0;
      boolean clash = false;
      for (int i = 0; i < o.files.length(); i++) {
        JSONObject f = o.files.getJSONObject(i);
        desc.append(f.getString("name")).append(" · ").append(f.getLong("size")).append(" 字节\n");
        total += f.getLong("size");
        clash |= new File(received, f.getString("name")).exists();
      }
      if (total + 128L * 1024 * 1024 > received.getUsableSpace()) throw new IOException("接收存储空间不足");
      if (!remembered(o.peer)) desc.append("\n首次连接，请核对双方验证码：\n").append(o.code);
      if (clash) desc.append("\n存在同名文件，接受将替换这些文件；取消则保留现有文件。");
      boolean yes = ui.confirm(clash ? "接收并覆盖同名文件？" : "接收文件？", desc.toString());
      synchronized (o) {
        if (!o.status.equals("pending") || o.expiry < now()) return;
        if (yes) {
          remember(o.peer);
          o.overwrite = clash;
          o.token = Protocol.random(24);
          o.status = "accepted";
          o.expiry = now() + 3600000;
          ui.status("正在接收文件，可点击取消");
        } else {
          o.status = "rejected";
          o.expiry = now() + 60000;
        }
      }
    } catch (Exception e) {
      abort(o);
      ui.status(e.getMessage());
    }
  }

  void upload(SSLSocket s, InputStream in, OutputStream out, Wire.Head h, String path, String ip)
      throws Exception {
    String[] p = path.split("/");
    if (p.length != 5 || !p[4].matches("[0-9]{1,2}")) throw new IOException("上传地址无效");
    Offer o = offers.get(p[3]);
    int index = Integer.parseInt(p[4]);
    File temp = null;
    if (o == null) throw new IOException("文件请求不存在");
    synchronized (o) {
      if (!o.status.equals("accepted")
          || o.expiry < now()
          || !o.peer.host.equals(ip)
          || !Protocol.equal(o.token, h.headers.get("x-transfer-token"))
          || index >= o.files.length()
          || o.complete.contains(index)
          || o.active.contains(index)) throw new IOException("接收未授权");
      if (o.files.getJSONObject(index).getLong("size") != h.length())
        throw new IOException("文件大小不一致");
      if (!o.active.isEmpty()) throw new IOException("请逐个发送文件");
      o.active.add(index);
      o.socket = s;
    }
    try {
      JSONObject f = o.files.getJSONObject(index);
      long remaining = h.length();
      if (remaining + 128L * 1024 * 1024 > received.getUsableSpace())
        throw new IOException("存储空间不足");
      temp = File.createTempFile(".receiving-", ".part", received);
      MessageDigest digest = MessageDigest.getInstance("SHA-256");
      byte[] b = new byte[65536];
      try (FileOutputStream output = new FileOutputStream(temp)) {
        while (remaining > 0) {
          if (closed || !o.status.equals("accepted") || o.expiry < now())
            throw new IOException("接收已取消或超时");
          int n = in.read(b, 0, (int) Math.min(b.length, remaining));
          if (n < 0) throw new EOFException("文件传输中断");
          output.write(b, 0, n);
          digest.update(b, 0, n);
          remaining -= n;
        }
        output.getFD().sync();
      }
      String hash = Protocol.hex(digest.digest());
      if (!Protocol.equal(hash, f.getString("sha256"))) throw new IOException("文件 SHA-256 不一致");
      synchronized (o) {
        if (!o.status.equals("accepted") || o.expiry < now()) throw new IOException("接收已取消");
        File target = new File(received, Protocol.name(f.getString("name")));
        ReceivedFiles.commit(temp, target, o.overwrite);
        temp = null;
        o.complete.add(index);
        if (o.complete.size() == o.files.length()) {
          o.status = "complete";
          o.token = "";
          o.expiry = now() + 60000;
        }
      }
      ui.message(o.peer, "收到文件：" + f.getString("name") + "（在接收文件中导出）", false);
      Wire.respond(out, 201, new JSONObject().put("sha256", hash));
    } catch (Exception e) {
      synchronized (o) {
        o.socket = null;
      }
      abort(o);
      ui.status("接收失败：" + e.getMessage());
      throw e;
    } finally {
      if (temp != null) temp.delete();
      synchronized (o) {
        o.active.remove(index);
        o.socket = null;
      }
    }
  }

  void abort(Offer o) {
    synchronized (o) {
      if (o.status.equals("complete")) return;
      o.status = "cancelled";
      o.token = "";
      o.expiry = now() + 60000;
      try {
        if (o.socket != null) o.socket.close();
      } catch (IOException ignored) {
      }
    }
  }

  public void cancel() {
    cancelSend.set(true);
    try {
      if (sendingSocket != null) sendingSocket.close();
    } catch (IOException ignored) {
    }
    Offer o = receiving;
    if (o != null) abort(o);
    ui.status("已取消当前传输");
  }

  public void manual(String ip, int port) {
    actions.execute(
        () -> {
          try {
            if (!Protocol.lan(ip) || port < 1024 || port > 65535)
              throw new IOException("请输入局域网 IPv4 和端口");
            Socket raw = new Socket();
            String pin;
            try {
              raw.bind(new InetSocketAddress(host, 0));
              raw.connect(new InetSocketAddress(ip, port), 5000);
              raw.setSoTimeout(10000);
              try (SSLSocket s =
                  (SSLSocket) tlsClient.getSocketFactory().createSocket(raw, ip, port, true)) {
                s.startHandshake();
                pin = Protocol.sha(s.getSession().getPeerCertificates()[0].getEncoded());
              }
            } finally {
              raw.close();
            }
            JSONObject provisional =
                new JSONObject()
                    .put("id", Protocol.random(16))
                    .put("name", "待验证")
                    .put("port", port)
                    .put("fingerprint", pin);
            Protocol.Peer p = new Protocol.Peer(provisional, ip);
            JSONObject hello = Wire.request(tlsClient, p, host, "GET", "/peer/hello", null, null);
            if (!hello.optString("app").equals("lindrop") || hello.optInt("protocol") != 2)
              throw new IOException("对方不是兼容邻传设备");
            p = new Protocol.Peer(hello, ip);
            remembered(p);
            track(p);
            if (manualPeers.size() >= 128) throw new IOException("手动设备已达上限");
            manualPeers.add(p.id);
            peers.put(p.id, p);
            ui.peers(new ArrayList<>(peers.values()));
            ui.status("已发现：" + p.name + "，首次仍需核对验证码");
          } catch (Exception e) {
            ui.status("连接失败：" + e.getMessage());
          }
        });
  }

  Outgoing connectChat(Protocol.Peer peer) throws Exception {
    // Do not hold this Node's monitor while calling the remote node: reverse
    // requests use the rate limiter's monitor and must remain able to run.
    synchronized (chatConnectLock) {
      return connectChatLocked(peer);
    }
  }

  Outgoing connectChatLocked(Protocol.Peer peer) throws Exception {
    remembered(peer);
    Outgoing old = outgoing.get(peer.id);
    if (old != null && old.active && old.expiry > now()) return old;
    if (old != null && old.poll != null) old.poll.cancel(false);
    String nonce = Protocol.random(32);
    JSONObject result =
        request(
            peer,
            "POST",
            "/peer/chat/offer",
            identity().put("files", new JSONArray()).put("nonce", nonce).put("automatic", true),
            null);
    String cid = result.getString("id"), secret = result.getString("secret");
    String code = Protocol.code(fp, peer.fp, nonce, cid);
    if (!cid.matches("[0-9a-f]{32}")
        || !secret.matches("[A-Za-z0-9_-]{30,64}")
        || !result.optString("receiver_id").equals(peer.id)
        || !Protocol.equal(result.optString("code"), code)) throw new IOException("连接验证信息不一致");
    boolean known = remembered(peer);
    if (!known && !ui.confirm("核对设备 · " + peer.name, "请核对双方显示相同验证码：\n\n" + code + "\n\n确认后记住设备。")) {
      request(peer, "POST", "/peer/chat/" + cid + "/close", new JSONObject(), secret);
      throw new IOException("已取消连接");
    }
    long deadline = now() + 120000;
    while (!closed && now() < deadline) {
      JSONObject state = request(peer, "GET", "/peer/chat/" + cid + "/0", null, secret);
      String status = state.getString("status");
      if (status.equals("accepted")) {
        if (!known) remember(peer);
        request(peer, "POST", "/peer/chat/" + cid + "/activate", new JSONObject(), secret);
        Outgoing c = new Outgoing();
        c.peer = peer;
        c.id = cid;
        c.secret = secret;
        c.active = true;
        outgoing.put(peer.id, c);
        c.poll = timers.scheduleWithFixedDelay(() -> poll(c), 0, 400, TimeUnit.MILLISECONDS);
        return c;
      }
      if (!status.equals("pending")) throw new IOException("对方已拒绝连接");
      Thread.sleep(400);
    }
    throw new IOException("等待连接确认超时");
  }

  void poll(Outgoing c) {
    if (closed || !c.active) return;
    try {
      JSONObject state =
          request(c.peer, "GET", "/peer/chat/" + c.id + "/" + c.cursor, null, c.secret);
      if (!state.getString("status").equals("active")) throw new IOException("聊天连接已结束，下次发送会重连");
      JSONArray msgs = state.getJSONArray("messages");
      for (int i = 0; i < msgs.length(); i++) {
        JSONObject m = msgs.getJSONObject(i);
        if (!m.getString("author").equals("initiator"))
          ui.message(c.peer, m.getString("text"), false);
        c.cursor = Math.max(c.cursor, m.getLong("seq"));
      }
    } catch (Exception e) {
      c.active = false;
      if (c.poll != null) c.poll.cancel(false);
      if (!closed) ui.status(e.getMessage());
    }
  }

  Channel incomingChat(Protocol.Peer peer) {
    return channels.values().stream()
        .filter(c -> c.peer.id.equals(peer.id) && c.status.equals("active") && c.expiry > now())
        .findFirst()
        .orElse(null);
  }

  public void sendChat(Protocol.Peer peer, String text) {
    actions.execute(
        () -> {
          try {
            Protocol.text(text);
            remembered(peer);
            Channel incoming = incomingChat(peer);
            if (incoming != null) {
              synchronized (incoming) {
                append(incoming, text, Protocol.random(16), "receiver");
              }
              return;
            }
            Outgoing c = connectChat(peer);
            request(
                peer,
                "POST",
                "/peer/chat/" + c.id + "/send",
                new JSONObject().put("text", text).put("message_id", Protocol.random(16)),
                c.secret);
            ui.message(peer, text, true);
          } catch (Exception e) {
            ui.status("消息未发送：" + e.getMessage());
          }
        });
  }

  public static class SendFile {
    public final File file;
    public final JSONObject meta;

    public SendFile(File file, String name) throws Exception {
      this.file = file;
      Protocol.name(name);
      long size = file.length();
      if (size > Protocol.MAX_FILE) throw new IOException("文件超过 2 GiB");
      MessageDigest d = MessageDigest.getInstance("SHA-256");
      try (InputStream in = new FileInputStream(file)) {
        byte[] b = new byte[65536];
        int n;
        while ((n = in.read(b)) != -1) d.update(b, 0, n);
      }
      meta =
          new JSONObject()
              .put("name", name)
              .put("size", size)
              .put("sha256", Protocol.hex(d.digest()));
    }
  }

  public void sendFiles(Protocol.Peer peer, List<SendFile> files) {
    if (!sending.compareAndSet(false, true)) {
      ui.status("已有发送任务");
      for (SendFile f : files) f.file.delete();
      return;
    }
    cancelSend.set(false);
    actions.execute(
        () -> {
          String oid = null, secret = null;
          try {
            remembered(peer);
            if (incomingChat(peer) == null) connectChat(peer);
            if (cancelSend.get()) throw new IOException("已取消发送");
            JSONArray data = new JSONArray();
            for (SendFile f : files) data.put(f.meta);
            Protocol.files(data);
            String nonce = Protocol.random(32);
            JSONObject result =
                request(
                    peer,
                    "POST",
                    "/peer/offer",
                    identity().put("files", data).put("nonce", nonce),
                    null);
            oid = result.getString("id");
            secret = result.getString("secret");
            if (!oid.matches("[0-9a-f]{32}")
                || !secret.matches("[A-Za-z0-9_-]{30,64}")
                || !result.optString("receiver_id").equals(peer.id)
                || !Protocol.equal(
                    result.optString("code"), Protocol.code(fp, peer.fp, nonce, oid)))
              throw new IOException("文件请求验证失败");
            ui.status("等待 " + peer.name + " 接收文件，可取消");
            String token = null;
            long deadline = now() + 120000;
            while (!closed && !cancelSend.get() && now() < deadline) {
              JSONObject state = request(peer, "GET", "/peer/offers/" + oid, null, secret);
              String status = state.getString("status");
              if (status.equals("accepted")) {
                token = state.getString("token");
                break;
              }
              if (!status.equals("pending")) throw new IOException("对方已拒绝或取消接收");
              Thread.sleep(400);
            }
            if (token == null) throw new IOException("发送已取消或等待超时");
            for (int i = 0; i < files.size(); i++) {
              SendFile f = files.get(i);
              if (closed || cancelSend.get()) throw new IOException("已取消发送");
              remembered(peer);
              try (SSLSocket s = Wire.connect(tlsClient, peer, host);
                  InputStream in = new FileInputStream(f.file)) {
                sendingSocket = s;
                Wire.requestHead(
                    s.getOutputStream(),
                    peer,
                    "POST",
                    "/peer/upload/" + oid + "/" + i,
                    f.file.length(),
                    "X-Transfer-Token",
                    token);
                byte[] b = new byte[65536];
                int n;
                long sent = 0, last = 0;
                while ((n = in.read(b)) != -1) {
                  if (closed || cancelSend.get()) throw new IOException("已取消发送");
                  s.getOutputStream().write(b, 0, n);
                  sent += n;
                  if (now() - last > 500) {
                    last = now();
                    ui.status(
                        "发送 "
                            + f.meta.getString("name")
                            + " · "
                            + sent
                            + " / "
                            + f.file.length()
                            + " 字节");
                  }
                }
                s.getOutputStream().flush();
                JSONObject reply = Wire.response(s.getInputStream());
                if (!Protocol.equal(reply.optString("sha256"), f.meta.getString("sha256")))
                  throw new IOException("接收文件校验失败");
                ui.message(peer, "已发送文件：" + f.meta.getString("name"), true);
              } finally {
                sendingSocket = null;
              }
            }
            ui.status("文件发送完成");
          } catch (Exception e) {
            if (oid != null && secret != null)
              try {
                request(peer, "POST", "/peer/cancel/" + oid, new JSONObject(), secret);
              } catch (Exception ignored) {
              }
            ui.status("文件未完成：" + e.getMessage());
          } finally {
            sending.set(false);
            for (SendFile f : files) f.file.delete();
          }
        });
  }

  @Override
  public void close() {
    closed = true;
    cancelSend.set(true);
    try {
      if (sendingSocket != null) sendingSocket.close();
    } catch (IOException ignored) {
    }
    Offer o = receiving;
    if (o != null) abort(o);
    try {
      if (listener != null) listener.close();
    } catch (IOException ignored) {
    }
    if (multicast != null) multicast.close();
    for (Socket s : sockets)
      try {
        s.close();
      } catch (IOException ignored) {
      }
    timers.shutdownNow();
    actions.shutdownNow();
    handlers.shutdownNow();
    reconnect.shutdownNow();
  }
}
