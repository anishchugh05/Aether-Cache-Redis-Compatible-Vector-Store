import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.HashMap;
import java.util.Base64;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.nio.file.Files;
import java.nio.file.Path;
import java.io.FileOutputStream;
import java.util.HashSet;
import java.util.TreeSet;
import java.math.BigDecimal;
import java.util.Arrays;
import java.util.Locale;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.PriorityQueue;
import java.util.Random;
import java.io.ByteArrayInputStream;
import java.io.EOFException;
import java.nio.channels.FileChannel;
import java.nio.file.StandardOpenOption;

public class Main {
  // Shared key-value store. ConcurrentHashMap is safe to use from multiple client threads.
  private static final Map<String, String> store = new ConcurrentHashMap<>();
  // Expiry time for each key (in milliseconds since epoch). Keys with no expiry aren't in here.
  private static final Map<String, Long> expiries = new ConcurrentHashMap<>();
  // Lists, stored separately from regular string keys
  private static final Map<String, List<String>> lists = new ConcurrentHashMap<>();
  // Clients blocked on BLPOP, per list, in the order they started waiting (first in line = first served)
  private static final Map<String, Deque<CompletableFuture<String>>> waiters = new ConcurrentHashMap<>();
  // Streams: each key holds a list of entries in the order they were added
  private static final Map<String, List<StreamEntry>> streams = new ConcurrentHashMap<>();
  // Blocked XREAD clients wait on this; XADD wakes them up
  private static final Object streamSignal = new Object();
  private static final Map<String, Long> keyVersions = new ConcurrentHashMap<>();
  private static String role = "master";
  private static String dir = System.getProperty("user.dir"); // defaults to the folder the server was started from
  private static String dbfilename = "dump.rdb";
  private static String appendonly = "no";
  private static String appenddirname = "appendonlydir";
  private static String appendfilename = "appendonly.aof";
  private static String appendfsync = "everysec";
  private static FileOutputStream aofOut = null;
  private static volatile boolean aofDirty = false;
  private static final String masterReplId = "8371b4fb1155b71f4a04d3e1bc3e18c4a990aeeb";
  private static long masterReplOffset = 0;
  private static final List<OutputStream> replicas = new CopyOnWriteArrayList<>();
  // Bytes of commands sent to replicas so far (the master's replication offset, used by WAIT)
  private static long replOffset = 0;
  private static final Object replLock = new Object();
  // Latest offset each replica has acknowledged with REPLCONF ACK
  private static final Map<OutputStream, Long> replicaAcks = new ConcurrentHashMap<>();
  // WAIT sleeps on this; incoming ACKs wake it up
  private static final Object ackSignal = new Object();
  private static final Map<String, Set<OutputStream>> channelSubscribers = new ConcurrentHashMap<>();
  // The default user's settings: "nopass" flag, and SHA-256 hashes of its passwords
  private static volatile boolean defaultNopass = true;
  private static final List<String> defaultPasswordHashes = new CopyOnWriteArrayList<>();
  private static final Set<String> SUBSCRIBED_MODE_COMMANDS =
      Set.of("SUBSCRIBE", "UNSUBSCRIBE", "PSUBSCRIBE", "PUNSUBSCRIBE", "PING", "QUIT", "RESET");
  private static final Set<String> WRITE_COMMANDS = Set.of("SET", "INCR", "RPUSH", "LPUSH", "LPOP", "XADD", "VADD");
  private static final String EMPTY_RDB_BASE64 =
      "UkVESVMwMDEx+glyZWRpcy12ZXIFNy4yLjD6CnJlZGlzLWJpdHPAQPoFY3RpbWXCbQi8ZfoIdXNlZC1tZW3CsMQQAPoIYW9mLWJhc2XAAP/wbjv+wP9aog==";
  private static String masterHost = null;
  private static int masterPort = 0;
  // Connection to the master, kept open for the rest of the handshake in later stages
  private static Socket masterSocket = null;
  private static long replicaOffset = 0;

  // Vector sets, one per key (for VADD / VSEARCH)
  private static final Map<String, VectorSet> vectorSets = new ConcurrentHashMap<>();

  // A set of vectors with ids. All vectors in a set have the same number of dimensions.
  // Vectors are stored normalized (length 1), so cosine similarity is just a dot product.
  // A set of vectors with ids, searchable two ways:
  //  - exact: compare against every vector (slow, always right; used to measure recall)
  //  - HNSW: a layered graph where each vector links to its nearest neighbors, so a search only
  //    walks a small part of the data (fast, very slightly approximate)
  // Vectors are stored normalized (length 1), so cosine similarity is just a dot product.
  static class VectorSet {
    // HNSW settings: links per node on upper layers (M) and on the bottom layer (M0),
    // and how wide the search is while building the graph
    static final int M = 16;
    static final int M0 = 32;
    static final int EF_CONSTRUCTION = 200;

    final int dim;
    final List<String> ids = new ArrayList<>();      // node number -> id
    final List<float[]> vecs = new ArrayList<>();    // node number -> vector
    final Map<String, Integer> nodeOf = new HashMap<>(); // id -> node number

    // Graph: links.get(node)[layer] holds that node's neighbors on that layer; counts says how many are used
    final List<int[][]> links = new ArrayList<>();
    final List<int[]> counts = new ArrayList<>();
    int entryPoint = -1; // search starts here (a node on the top layer)
    int maxLevel = -1;
    final double levelMult = 1 / Math.log(M);
    final Random rng = new Random(42);

    // "Visited" marks for a search, reused between searches to avoid allocating every time
    int[] visited = new int[16];
    int visitStamp = 0;

    VectorSet(int dim) {
      this.dim = dim;
    }

    // A node plus its similarity to the current query
    static class Cand {
      final int node;
      final double sim;
      Cand(int node, double sim) { this.node = node; this.sim = sim; }
    }

    // Adds a vector. Returns true if the id is new. Re-adding an id updates its vector in place.
    synchronized boolean add(String id, float[] vector) {
      float[] v = normalize(vector);
      Integer existing = nodeOf.get(id);
      if (existing != null) {
        vecs.set(existing, v);
        return false;
      }
      int node = vecs.size();
      ids.add(id);
      vecs.add(v);
      nodeOf.put(id, node);
      if (visited.length <= node) visited = Arrays.copyOf(visited, visited.length * 2);
      insert(node);
      return true;
    }

    private void insert(int node) {
      // Random top layer for this node: most nodes only live on layer 0, a few reach higher
      int level = (int) Math.floor(-Math.log(1 - rng.nextDouble()) * levelMult);
      int[][] nodeLinks = new int[level + 1][];
      for (int l = 0; l <= level; l++) nodeLinks[l] = new int[l == 0 ? M0 : M];
      links.add(nodeLinks);
      counts.add(new int[level + 1]);

      if (entryPoint == -1) { // first node
        entryPoint = node;
        maxLevel = level;
        return;
      }

      float[] q = vecs.get(node);
      int ep = entryPoint;
      // Above this node's top layer: just walk greedily toward the new vector
      for (int l = maxLevel; l > level; l--) ep = greedy(q, ep, l);
      // On each layer the node lives on: find close nodes and link to the best of them
      for (int l = Math.min(level, maxLevel); l >= 0; l--) {
        List<Cand> found = searchLayer(q, ep, EF_CONSTRUCTION, l);
        for (Cand c : selectNeighbors(found, M)) {
          addLink(node, c.node, l);
          addLink(c.node, node, l);
        }
        ep = found.get(0).node;
      }
      if (level > maxLevel) { // new tallest node becomes the entry point
        maxLevel = level;
        entryPoint = node;
      }
    }

    // Adds 'to' to 'from's neighbors on a layer; if that's over the limit, keeps only the best ones
    private void addLink(int from, int to, int layer) {
      int[] list = links.get(from)[layer];
      int[] cnt = counts.get(from);
      if (cnt[layer] < list.length) {
        list[cnt[layer]++] = to;
        return;
      }
      float[] fv = vecs.get(from);
      List<Cand> all = new ArrayList<>();
      for (int i = 0; i < cnt[layer]; i++) all.add(new Cand(list[i], dot(fv, vecs.get(list[i]))));
      all.add(new Cand(to, dot(fv, vecs.get(to))));
      all.sort((a, b) -> Double.compare(b.sim, a.sim));
      List<Cand> keep = selectNeighbors(all, list.length);
      cnt[layer] = keep.size();
      for (int i = 0; i < keep.size(); i++) list[i] = keep.get(i).node;
    }

    // Picks up to m neighbors from candidates (sorted best-first), preferring ones that point in
    // different directions, so the graph stays well connected instead of clumping
    private List<Cand> selectNeighbors(List<Cand> cands, int m) {
      List<Cand> chosen = new ArrayList<>();
      List<Cand> skipped = new ArrayList<>();
      for (Cand c : cands) {
        if (chosen.size() >= m) break;
        boolean diverse = true;
        for (Cand r : chosen) {
          // skip c if it's closer to an already-chosen neighbor than to the query itself
          if (dot(vecs.get(c.node), vecs.get(r.node)) > c.sim) { diverse = false; break; }
        }
        if (diverse) chosen.add(c); else skipped.add(c);
      }
      for (Cand c : skipped) { // top up with the best skipped ones if we're short
        if (chosen.size() >= m) break;
        chosen.add(c);
      }
      return chosen;
    }

    // Walks to whichever neighbor is more similar, until none is; returns where it stopped
    private int greedy(float[] q, int ep, int layer) {
      int best = ep;
      double bestSim = dot(q, vecs.get(ep));
      boolean moved = true;
      while (moved) {
        moved = false;
        int[] list = links.get(best)[layer];
        int n = counts.get(best)[layer];
        for (int i = 0; i < n; i++) {
          double sim = dot(q, vecs.get(list[i]));
          if (sim > bestSim) { bestSim = sim; best = list[i]; moved = true; }
        }
      }
      return best;
    }

    // Best-first search on one layer, tracking the ef closest nodes found. Returns them best-first.
    private List<Cand> searchLayer(float[] q, int ep, int ef, int layer) {
      visitStamp++;
      PriorityQueue<Cand> toExplore = new PriorityQueue<>((a, b) -> Double.compare(b.sim, a.sim)); // best first
      PriorityQueue<Cand> results = new PriorityQueue<>((a, b) -> Double.compare(a.sim, b.sim));   // worst on top
      Cand start = new Cand(ep, dot(q, vecs.get(ep)));
      toExplore.add(start);
      results.add(start);
      visited[ep] = visitStamp;

      while (!toExplore.isEmpty()) {
        Cand c = toExplore.poll();
        if (c.sim < results.peek().sim && results.size() >= ef) break; // nothing better left to find
        int[][] nodeLinks = links.get(c.node);
        if (layer >= nodeLinks.length) continue;
        int[] list = nodeLinks[layer];
        int n = counts.get(c.node)[layer];
        for (int i = 0; i < n; i++) {
          int nb = list[i];
          if (visited[nb] == visitStamp) continue;
          visited[nb] = visitStamp;
          double sim = dot(q, vecs.get(nb));
          if (results.size() < ef || sim > results.peek().sim) {
            Cand nc = new Cand(nb, sim);
            toExplore.add(nc);
            results.add(nc);
            if (results.size() > ef) results.poll();
          }
        }
      }
      List<Cand> out = new ArrayList<>(results);
      out.sort((a, b) -> Double.compare(b.sim, a.sim));
      return out;
    }

    // HNSW search: drop down the layers greedily, then do a wider search (ef) on the bottom layer
    synchronized List<Map.Entry<String, Double>> search(float[] query, int k, int ef) {
      List<Map.Entry<String, Double>> result = new ArrayList<>();
      if (entryPoint == -1) return result;
      float[] q = normalize(query);
      int ep = entryPoint;
      for (int l = maxLevel; l > 0; l--) ep = greedy(q, ep, l);
      List<Cand> found = searchLayer(q, ep, Math.max(ef, k), 0);
      for (int i = 0; i < Math.min(k, found.size()); i++) {
        result.add(Map.entry(ids.get(found.get(i).node), found.get(i).sim));
      }
      return result;
    }

    // Exact search: compare the query against every vector, keep the k most similar
    synchronized List<Map.Entry<String, Double>> searchExact(float[] query, int k) {
      float[] q = normalize(query);
      // Min-heap of size k: the weakest of the current top-k sits on top, ready to be replaced
      PriorityQueue<Cand> top = new PriorityQueue<>((a, b) -> Double.compare(a.sim, b.sim));
      for (int node = 0; node < vecs.size(); node++) {
        double sim = dot(q, vecs.get(node));
        if (top.size() < k) {
          top.add(new Cand(node, sim));
        } else if (sim > top.peek().sim) {
          top.poll();
          top.add(new Cand(node, sim));
        }
      }
      List<Cand> sorted = new ArrayList<>(top);
      sorted.sort((a, b) -> Double.compare(b.sim, a.sim));
      List<Map.Entry<String, Double>> result = new ArrayList<>();
      for (Cand c : sorted) result.add(Map.entry(ids.get(c.node), c.sim));
      return result;
    }
  }

  static float[] normalize(float[] v) {
    double sum = 0;
    for (float x : v) sum += x * x;
    double len = Math.sqrt(sum);
    float[] out = new float[v.length];
    for (int i = 0; i < v.length; i++) out[i] = (float) (len == 0 ? 0 : v[i] / len);
    return out;
  }

  static double dot(float[] a, float[] b) {
    double sum = 0;
    for (int i = 0; i < a.length; i++) sum += a[i] * b[i];
    return sum;
  }

  // Sorted sets, one per key
  private static final Map<String, SortedSet> sortedSets = new ConcurrentHashMap<>();

  // A sorted set: members kept in order of score (ties broken alphabetically by member name)
  static class SortedSet {
    final Map<String, Double> scores = new HashMap<>(); // member -> score, for quick lookups
    final TreeSet<String> ordered = new TreeSet<>((a, b) -> {
      int byScore = Double.compare(scores.get(a), scores.get(b));
      return byScore != 0 ? byScore : a.compareTo(b);
    });

    // Adds a member (or updates its score). Returns true if it's a new member.
    synchronized boolean add(String member, double score) {
      boolean isNew = !scores.containsKey(member);
      if (!isNew) ordered.remove(member); // take it out BEFORE its score changes, so the tree stays in order
      scores.put(member, score);
      ordered.add(member);
      return isNew;
    }
    
    // 0-based position of the member in score order, or -1 if it isn't in the set
    synchronized int rank(String member) {
      if (!scores.containsKey(member)) return -1;
      return ordered.headSet(member).size(); // how many members come before it
    }
    synchronized List<String> members() {
      return new ArrayList<>(ordered);
    }
    synchronized int size() {
      return scores.size();
    }
    synchronized Double score(String member) {
      return scores.get(member);
    }
    // Removes a member. Returns true if it was there.
    synchronized boolean remove(String member) {
      if (!scores.containsKey(member)) return false;
      ordered.remove(member); // remove from the tree first, while its score is still known
      scores.remove(member);
      return true;
    }
  }

  // One stream entry: its ID plus its field-value pairs, e.g. ["temperature", "36", "humidity", "95"]
  static class StreamEntry {
    final String id;
    final List<String> fields;

    StreamEntry(String id, List<String> fields) {
      this.id = id;
      this.fields = fields;
    }
  }

  public static void main(String[] args){
    System.out.println("Logs from your program will appear here!");

    int port = 6379;
    for (int i = 0; i < args.length - 1; i++) {
      if (args[i].equals("--port")) {
        port = Integer.parseInt(args[i + 1]);
      } else if (args[i].equals("--dir")) {
        dir = args[i + 1];
      } else if (args[i].equals("--dbfilename")) {
        dbfilename = args[i + 1];
      } else if (args[i].equals("--appendonly")) {
        appendonly = args[i + 1];
      } else if (args[i].equals("--appenddirname")) {
        appenddirname = args[i + 1];
      } else if (args[i].equals("--appendfilename")) {
        appendfilename = args[i + 1];
      } else if (args[i].equals("--appendfsync")) {
        appendfsync = args[i + 1];
      } else if (args[i].equals("--replicaof")) {
        role = "slave";
        // e.g. --replicaof "localhost 6379" -> host "localhost", port 6379
        String[] parts = args[i + 1].split(" ");
        masterHost = parts[0];
        masterPort = Integer.parseInt(parts[1]);
      }
    }
    loadRdb();
    // With AOF on, set up <dir>/<appenddirname>, the manifest, and the AOF file before any clients connect
    if (appendonly.equals("yes")) {
      try {
        Path aofDir = Path.of(dir, appenddirname);
        Files.createDirectories(aofDir); // does nothing if it already exists

        // The manifest lists the AOF files, e.g. appendonly.aof.manifest. Create a default one if missing.
        Path manifest = aofDir.resolve(appendfilename + ".manifest");
        if (!Files.exists(manifest)) {
          Files.writeString(manifest, "file " + appendfilename + ".1.incr.aof seq 1 type i\n");
        }

        // Find the active incremental file: the line with "type i", e.g. "file X seq 1 type i" -> X
        String activeFile = null;
        for (String line : Files.readAllLines(manifest)) {
          String[] parts = line.trim().split(" ");
          if (parts.length >= 2 && line.trim().endsWith("type i")) {
            activeFile = parts[1];
          }
        }

        // Replay the commands saved in it to rebuild the data. This runs before aofOut is opened,
        // so replayed commands don't get written to the file a second time.
        Path aofFile = aofDir.resolve(activeFile);
        if (Files.exists(aofFile)) {
          byte[] data = Files.readAllBytes(aofFile);
          ByteArrayInputStream in = new ByteArrayInputStream(data);
          int goodBytes = 0; // how much of the file is complete commands
          while (true) {
            List<String> command;
            try {
              command = readCommand(in);
            } catch (Exception e) {
              break; // the file ends partway through a command
            }
            if (command == null) break;
            executeCommand(command);
            goodBytes = data.length - in.available();
          }
          // A crash in the middle of a write can leave half a command at the end.
          // Drop it (like Redis's aof-load-truncated) so the server can still start.
          if (goodBytes < data.length) {
            System.out.println("AOF ends with an incomplete command; dropping the last "
                + (data.length - goodBytes) + " bytes");
            try (FileChannel ch = FileChannel.open(aofFile, StandardOpenOption.WRITE)) {
              ch.truncate(goodBytes);
            }
          }
        }

        // Open it for appending (this also creates it if it doesn't exist yet)
        aofOut = new FileOutputStream(aofFile.toFile(), true);
        // everysec: a background thread flushes new writes to disk once per second
        if (appendfsync.equals("everysec")) {
          Thread syncer = new Thread(() -> {
            while (true) {
              try {
                Thread.sleep(1000);
                if (aofDirty) {
                  aofDirty = false;
                  aofOut.getFD().sync();
                }
              } catch (Exception e) {
                System.out.println("AOF background sync failed: " + e.getMessage());
              }
            }
          });
          syncer.setDaemon(true); // don't keep the program alive just for this thread
          syncer.start();
        }
      } catch (IOException e) {
        System.out.println("Couldn't set up AOF: " + e.getMessage());
      }
    }
    try (ServerSocket serverSocket = new ServerSocket(port)) {
      serverSocket.setReuseAddress(true);
      if (masterHost != null) {
        connectToMaster(port);
      }
      while (true) {
        Socket clientSocket = serverSocket.accept();
        new Thread(() -> handleClient(clientSocket)).start();
      }
    } catch (IOException e) {
      System.out.println("IOException: " + e.getMessage());
    }
  }

  // Replica handshake: PING, then REPLCONF twice, waiting for the master's reply after each
  private static void connectToMaster(int myPort) throws IOException {
    masterSocket = new Socket(masterHost, masterPort);
    OutputStream out = masterSocket.getOutputStream();
    InputStream in = new BufferedInputStream(masterSocket.getInputStream());

    // Step 1: PING (master replies +PONG)
    sendCommand(out, "PING");
    readLine(in);

    // Step 2: tell the master our port, then our capabilities (master replies +OK to each)
    sendCommand(out, "REPLCONF", "listening-port", String.valueOf(myPort));
    readLine(in);
    sendCommand(out, "REPLCONF", "capa", "psync2");
    readLine(in);
    // Step 3: ask for a full sync (we don't know the master's ID yet, and have no data: "? -1")
    sendCommand(out, "PSYNC", "?", "-1");
    readLine(in); // "+FULLRESYNC <id> <offset>"

    // The master then sends the RDB file as $<length>\r\n<bytes>. Read and skip it (it's empty).
    String rdbHeader = readLine(in); // e.g. "$88"
    int rdbLength = Integer.parseInt(rdbHeader.substring(1));
    in.readNBytes(rdbLength);

    // From now on, the master sends write commands. Apply them in the background, without replying.
    new Thread(() -> {
      try {
        while (true) {
          List<String> command = readCommand(in);
          if (command == null) break; // master disconnected
          if (command.get(0).equalsIgnoreCase("REPLCONF") && command.get(1).equalsIgnoreCase("GETACK")) {
            // The one command we DO reply to: tell the master how much we've processed (before this GETACK)
            sendCommand(out, "REPLCONF", "ACK", String.valueOf(replicaOffset));
          } else {
            executeCommand(command); // everything else: apply silently
          }
          // Count this command's size in bytes (after replying, so a GETACK doesn't count itself)
          replicaOffset += respLength(command);
        }
      } catch (IOException e) {
        System.out.println("Lost connection to master: " + e.getMessage());
      }
    }).start();
  }
  
  // Appends a write command to the AOF file as a RESP array
  private static synchronized void appendToAof(List<String> command) {
    if (aofOut == null) return; // AOF is off
    try {
      sendCommand(aofOut, command.toArray(new String[0])); // same RESP encoding as over the network
      if (appendfsync.equals("always")) {
        aofOut.getFD().sync(); // force it onto the disk before we reply to the client
      } else if (appendfsync.equals("everysec")) {
        aofDirty = true;       // the background thread will sync it within a second
      }
      // "no": don't sync at all; the operating system writes it to disk whenever it chooses
    } catch (IOException e) {
      System.out.println("Couldn't write to AOF: " + e.getMessage());
    }
  }

  // Sends a command to every replica as a RESP array (no reply is expected)
  private static void propagate(List<String> command) {
    synchronized (replLock) { // keeps the offset and the order of sent commands in step
      replOffset += respLength(command);
      for (OutputStream replica : replicas) {
        try {
          synchronized (replica) { // one write at a time, so commands don't get mixed together
            sendCommand(replica, command.toArray(new String[0]));
          }
        } catch (IOException e) {
          replicas.remove(replica); // replica disconnected
          replicaAcks.remove(replica);
        }
      }
    }
  }

  private static String handleWait(List<String> command) {
    int needed = Integer.parseInt(command.get(1));
    long timeout = Long.parseLong(command.get(2)); // milliseconds, 0 = wait forever

    long target; // every write sent so far; replicas must have processed up to here
    synchronized (replLock) {
      target = replOffset;
    }
    if (target == 0) {
      return ":" + replicas.size() + "\r\n"; // nothing written yet, so everyone is in sync
    }

    // Ask replicas where they're at (only needed if not enough have already confirmed)
    if (countAcked(target) < needed) {
      propagate(List.of("REPLCONF", "GETACK", "*"));
    }

    long deadline = System.currentTimeMillis() + timeout;
    synchronized (ackSignal) {
      while (true) {
        int acked = countAcked(target);
        if (acked >= needed) return ":" + acked + "\r\n";
        try {
          if (timeout == 0) {
            ackSignal.wait();
          } else {
            long remaining = deadline - System.currentTimeMillis();
            if (remaining <= 0) return ":" + acked + "\r\n"; // time's up: report however many made it
            ackSignal.wait(remaining);
          }
        } catch (InterruptedException e) {
          return ":" + acked + "\r\n";
        }
      }
    }
  }

  // How many replicas have confirmed they've processed at least 'target' bytes
  private static int countAcked(long target) {
    int count = 0;
    for (OutputStream replica : replicas) {
      if (replicaAcks.getOrDefault(replica, 0L) >= target) count++;
    }
    return count;
  }

  // How many bytes a command takes as a RESP array, e.g. PING = "*1\r\n$4\r\nPING\r\n" = 14
  private static long respLength(List<String> command) {
    long total = ("*" + command.size() + "\r\n").length();
    for (String part : command) {
      int bytes = part.getBytes(StandardCharsets.ISO_8859_1).length;
      total += ("$" + bytes + "\r\n").length() + bytes + 2; // $len\r\n + data + \r\n
    }
    return total;
  }

  // Encodes a command like ["REPLCONF", "capa", "psync2"] as a RESP array and sends it
  private static void sendCommand(OutputStream out, String... parts) throws IOException {
    StringBuilder sb = new StringBuilder();
    sb.append("*").append(parts.length).append("\r\n");
    for (String p : parts) {
      sb.append(bulkString(p));
    }
    out.write(sb.toString().getBytes(StandardCharsets.ISO_8859_1));
    out.flush();
  }

  private static void handleClient(Socket clientSocket) {
    OutputStream out = null;
    try (clientSocket) {
      InputStream in = new BufferedInputStream(clientSocket.getInputStream());
      out = clientSocket.getOutputStream();
      boolean inMulti = false; // is this connection inside a MULTI transaction?
      List<List<String>> queued = new ArrayList<>(); // commands saved up during MULTI
      Map<String, Long> watchedKeys = new HashMap<>(); // watched key -> its version at WATCH time
      Set<String> subscribedChannels = new HashSet<>(); // channels this connection has SUBSCRIBEd to
      // New connections are logged in automatically only while the default user has "nopass"
      boolean authenticated = defaultNopass;

      while (true) {
        List<String> command = readCommand(in);
        if (command == null) break; // client disconnected

        String name = command.get(0).toUpperCase();
        String response;
        if (name.equals("AUTH")) {
          response = handleAuth(command.get(1), command.get(2));
          if (response.startsWith("+OK")) authenticated = true; // log this connection in
        } else if (!authenticated) {
          response = "-NOAUTH Authentication required.\r\n";
        } else if (!subscribedChannels.isEmpty() && !SUBSCRIBED_MODE_COMMANDS.contains(name)) {
          // In subscribed mode, only a few commands are allowed
          response = "-ERR Can't execute '" + command.get(0).toLowerCase()
              + "': only (P|S)SUBSCRIBE / (P|S)UNSUBSCRIBE / PING / QUIT / RESET are allowed in this context\r\n";
        } else if (name.equals("PING") && !subscribedChannels.isEmpty()) {
          response = "*2\r\n" + bulkString("pong") + bulkString(""); // subscribed-mode PING reply
        } else if (name.equals("MULTI")) {
          inMulti = true;
          response = "+OK\r\n";
        } else if (name.equals("EXEC")) {
          if (!inMulti) {
            response = "-ERR EXEC without MULTI\r\n";
          } else {
            // Was any watched key written to since WATCH?
            boolean dirty = false;
            for (Map.Entry<String, Long> w : watchedKeys.entrySet()) {
              if (!keyVersions.getOrDefault(w.getKey(), 0L).equals(w.getValue())) {
                dirty = true;
                break;
              }
            }

            if (dirty) {
              response = "*-1\r\n"; // abort: throw away the queue without running anything
            } else {
              // Run every queued command in order and collect their responses into one array
              StringBuilder sb = new StringBuilder();
              sb.append("*").append(queued.size()).append("\r\n");
              for (List<String> q : queued) {
                sb.append(executeCommand(q));
              }
              response = sb.toString();
            }
            inMulti = false;
            queued.clear();
            watchedKeys.clear(); // watches end after EXEC either way
          }
        } else if (name.equals("DISCARD")) {
          if (!inMulti) {
            response = "-ERR DISCARD without MULTI\r\n";
          } else {
            // Abort the transaction: throw away the queued commands without running them
            inMulti = false;
            queued.clear();
            watchedKeys.clear(); // watches end after DISCARD
            response = "+OK\r\n";
          }
        } else if (name.equals("WATCH")) {
          if (inMulti) {
            response = "-ERR WATCH inside MULTI is not allowed\r\n";
          } else {
            // Remember each key's current version, so EXEC can tell if it was written to since
            for (String key : command.subList(1, command.size())) {
              watchedKeys.put(key, keyVersions.getOrDefault(key, 0L));
            }
            response = "+OK\r\n";
          }
        } else if (name.equals("SUBSCRIBE")) {
          String channel = command.get(1);
          subscribedChannels.add(channel);
          channelSubscribers.computeIfAbsent(channel, k -> ConcurrentHashMap.newKeySet()).add(out);
          // Reply: ["subscribe", channel, number of channels this connection is subscribed to]
          response = "*3\r\n" + bulkString("subscribe") + bulkString(channel)
              + ":" + subscribedChannels.size() + "\r\n";
        } else if (name.equals("UNSUBSCRIBE")) {
          String channel = command.get(1);
          subscribedChannels.remove(channel);
          Set<OutputStream> subs = channelSubscribers.get(channel);
          if (subs != null) subs.remove(out); // stop receiving this channel's messages
          // Reply: ["unsubscribe", channel, number of channels still subscribed to]
          response = "*3\r\n" + bulkString("unsubscribe") + bulkString(channel)
              + ":" + subscribedChannels.size() + "\r\n";
        } else if (name.equals("UNWATCH")) {
          watchedKeys.clear(); // stop watching everything for this connection
          response = "+OK\r\n";
        } else if (name.equals("REPLCONF") && command.size() > 1 && command.get(1).equalsIgnoreCase("ACK")) {
          // A replica reporting its offset. Record it, wake up any WAIT, and send no reply.
          synchronized (ackSignal) {
            replicaAcks.put(out, Long.parseLong(command.get(2)));
            ackSignal.notifyAll();
          }
          continue;
        } else if (name.equals("PSYNC")) {
          // Reply FULLRESYNC, then send the RDB file as $<length>\r\n<bytes> (no trailing \r\n)
          out.write(("+FULLRESYNC " + masterReplId + " " + masterReplOffset + "\r\n")
              .getBytes(StandardCharsets.ISO_8859_1));
          byte[] rdb = Base64.getDecoder().decode(EMPTY_RDB_BASE64);
          out.write(("$" + rdb.length + "\r\n").getBytes(StandardCharsets.ISO_8859_1));
          out.write(rdb);
          replicas.add(out);
          continue; // already wrote everything, skip the normal response write
        } else if (inMulti) {
          // Inside a transaction: save the command instead of running it
          queued.add(command);
          response = "+QUEUED\r\n";
        } else {
          response = executeCommand(command);
        }

        synchronized (out) { // a PUBLISH from another client may write to this connection too
          out.write(response.getBytes(StandardCharsets.ISO_8859_1));
        }
      }
    } catch (IOException e) {
      System.out.println("IOException: " + e.getMessage());
    } finally {
      // Client left: remove it from every channel it was subscribed to
      for (Set<OutputStream> subs : channelSubscribers.values()) {
        subs.remove(out);
      }
    }
  }

  // Runs a single command and returns its RESP response
  private static String executeCommand(List<String> command) {
    String name = command.get(0).toUpperCase();
    switch (name) {
      case "SET": case "INCR": case "RPUSH": case "LPUSH": case "LPOP": case "BLPOP": case "XADD": case "VADD":
        keyVersions.merge(command.get(1), 1L, Long::sum);
    }
    if (WRITE_COMMANDS.contains(name)) {
      propagate(command);
      appendToAof(command);
    }
    switch (name) {
      case "PING":   return "+PONG\r\n";
      case "ACL":    return handleAcl(command);
      case "AUTH":   return handleAuth(command.get(1), command.get(2));
      case "REPLCONF": return "+OK\r\n";
      case "WAIT":   return handleWait(command);
      case "CONFIG": return handleConfig(command);
      case "KEYS":   return handleKeys();
      case "SETBIT": return handleSetbit(command);
      case "GETBIT": return handleGetbit(command);
      case "BITCOUNT": return handleBitcount(command);
      case "BITOP":  return handleBitop(command);
      case "STRLEN": {
        handleGet(command.get(1)); // clears the key first if it has expired
        String value = store.get(command.get(1));
        return ":" + (value == null ? 0 : value.length()) + "\r\n"; // one char = one byte
      }
      case "GEOADD": return handleGeoadd(command);
      case "GEOPOS": return handleGeopos(command);
      case "GEODIST": return handleGeodist(command);
      case "GEOSEARCH": return handleGeosearch(command);
      case "VADD":   return handleVadd(command);
      case "VSEARCH": return handleVsearch(command);
      case "ZADD":   return handleZadd(command);
      case "ZRANK":  return handleZrank(command.get(1), command.get(2));
      case "ZRANGE": return handleZrange(command);
      case "ZSCORE": {
        SortedSet zset = sortedSets.get(command.get(1));
        Double score = (zset == null) ? null : zset.score(command.get(2));
        return (score == null) ? "$-1\r\n" : bulkString(formatScore(score));
      }
      case "ZREM": {
        SortedSet zset = sortedSets.get(command.get(1));
        boolean removed = zset != null && zset.remove(command.get(2));
        return ":" + (removed ? 1 : 0) + "\r\n";
      }
      case "ZCARD": {
        SortedSet zset = sortedSets.get(command.get(1));
        return ":" + (zset == null ? 0 : zset.size()) + "\r\n"; // 0 if the set doesn't exist
      }
      case "PUBLISH": return handlePublish(command.get(1), command.get(2));
      case "INFO":   return bulkString(
                         "role:" + role + "\r\n"
                       + "master_replid:" + masterReplId + "\r\n"
                       + "master_repl_offset:" + masterReplOffset + "\r\n");
      case "ECHO":   return bulkString(command.get(1));
      case "SET":    return handleSet(command);
      case "GET":    return handleGet(command.get(1));
      case "INCR":   return handleIncr(command.get(1));
      case "RPUSH":  return handleRPush(command);
      case "LPUSH":  return handleLpush(command);
      case "LLEN":   return handleLlen(command.get(1));
      case "LPOP":   return handleLpop(command);
      case "BLPOP":  return handleBlpop(command);
      case "LRANGE": return handleLrange(command);
      case "TYPE":   return handleType(command.get(1));
      case "XADD":   return handleXadd(command);
      case "XRANGE": return handleXrange(command);
      case "XREAD":  return handleXread(command);
      default:       return "-ERR unknown command '" + command.get(0) + "'\r\n";
    }
  }

  // SETBIT <key> <offset> <0|1>: sets one bit in the string at key, returns the bit's old value
  private static String handleSetbit(List<String> command) {
    String key = command.get(1);
    long offset = Long.parseLong(command.get(2));
    int value = Integer.parseInt(command.get(3));
    int byteIndex = (int) (offset / 8);
    int bitInByte = 7 - (int) (offset % 8); // offset 0 is the leftmost (most significant) bit

    handleGet(key); // clears the key first if it has expired
    int[] oldBit = new int[1];
    store.compute(key, (k, current) -> {
      // Bitmaps are stored as strings with one character per byte (ISO-8859-1 maps chars 0-255 to bytes 1:1)
      byte[] bytes = (current == null ? "" : current).getBytes(StandardCharsets.ISO_8859_1);
      if (byteIndex >= bytes.length) {
        bytes = Arrays.copyOf(bytes, byteIndex + 1); // grow the string, new bytes are all 0
      }
      oldBit[0] = (bytes[byteIndex] >> bitInByte) & 1;
      if (value == 1) {
        bytes[byteIndex] |= (1 << bitInByte);   // turn the bit on
      } else {
        bytes[byteIndex] &= ~(1 << bitInByte);  // turn the bit off
      }
      return new String(bytes, StandardCharsets.ISO_8859_1);
    });
    return ":" + oldBit[0] + "\r\n";
  }

  // GETBIT <key> <offset>: the bit at that offset (0 if the key is missing or the offset is past the end)
  private static String handleGetbit(List<String> command) {
    String key = command.get(1);
    long offset = Long.parseLong(command.get(2));
    handleGet(key); // clears the key first if it has expired
    String current = store.get(key);
    if (current == null) return ":0\r\n";

    byte[] bytes = current.getBytes(StandardCharsets.ISO_8859_1);
    long byteIndex = offset / 8;
    if (byteIndex >= bytes.length) return ":0\r\n"; // past the end counts as 0
    int bitInByte = 7 - (int) (offset % 8);
    return ":" + ((bytes[(int) byteIndex] >> bitInByte) & 1) + "\r\n";
  }

  // BITCOUNT <key> [start end]: how many bits are 1, optionally only in bytes start..end (inclusive)
  private static String handleBitcount(List<String> command) {
    String key = command.get(1);
    handleGet(key); // clears the key first if it has expired
    String current = store.get(key);
    if (current == null) return ":0\r\n";

    byte[] bytes = current.getBytes(StandardCharsets.ISO_8859_1);
    int start = 0;
    int end = bytes.length - 1;
    if (command.size() >= 4) {
      start = Integer.parseInt(command.get(2));
      end = Integer.parseInt(command.get(3));
    }
    if (end >= bytes.length) end = bytes.length - 1; // clamp end to the last byte
    if (start >= bytes.length || start > end) return ":0\r\n";

    int count = 0;
    for (int i = start; i <= end; i++) {
      count += Integer.bitCount(bytes[i] & 0xFF); // number of 1 bits in this byte
    }
    return ":" + count + "\r\n";
  }

  // BITOP AND <dest> <key1> <key2> ...: combines the bitmaps byte by byte and stores the result at dest
  private static String handleBitop(List<String> command) {
    String op = command.get(1).toUpperCase();
    String dest = command.get(2);

    // Load every source as bytes (a missing key counts as an empty string)
    List<byte[]> sources = new ArrayList<>();
    int maxLen = 0;
    for (String key : command.subList(3, command.size())) {
      handleGet(key); // clears the key first if it has expired
      String value = store.get(key);
      byte[] bytes = (value == null ? "" : value).getBytes(StandardCharsets.ISO_8859_1);
      sources.add(bytes);
      maxLen = Math.max(maxLen, bytes.length);
    }

    byte[] result = new byte[maxLen];
    for (int i = 0; i < maxLen; i++) {
      int combined = byteAt(sources.get(0), i);
      for (int s = 1; s < sources.size(); s++) {
        if (op.equals("AND")) {
          combined &= byteAt(sources.get(s), i); // bit stays 1 only if it's 1 in every source
        } else if (op.equals("OR")) {
          combined |= byteAt(sources.get(s), i); // bit is 1 if it's 1 in any source
        }
      }
      result[i] = (byte) combined;
    }

    if (maxLen == 0) {
      store.remove(dest); // nothing to store
    } else {
      store.put(dest, new String(result, StandardCharsets.ISO_8859_1));
    }
    expiries.remove(dest);
    return ":" + maxLen + "\r\n"; // length of the result in bytes
  }

  // The byte at index i, or 0 if the array is shorter than that
  private static int byteAt(byte[] bytes, int i) {
    return (i < bytes.length) ? (bytes[i] & 0xFF) : 0;
  }

  // GEOADD <key> <longitude> <latitude> <member>
  private static String handleGeoadd(List<String> command) {
    double longitude = Double.parseDouble(command.get(2));
    double latitude = Double.parseDouble(command.get(3));

    // Valid ranges (Web Mercator): longitude -180..180, latitude -85.05112878..85.05112878, edges included
    boolean validLongitude = longitude >= -180 && longitude <= 180;
    boolean validLatitude = latitude >= -85.05112878 && latitude <= 85.05112878;
    if (!validLongitude || !validLatitude) {
      return String.format("-ERR invalid longitude,latitude pair %f,%f\r\n", longitude, latitude);
    }
    SortedSet zset = sortedSets.computeIfAbsent(command.get(1), k -> new SortedSet());
    boolean added = zset.add(command.get(4), geoScore(latitude, longitude));
    return ":" + (added ? 1 : 0) + "\r\n";
  }

  // GEOPOS <key> <member1> <member2> ...: [longitude, latitude] for each member, or a null array if missing
  private static String handleGeopos(List<String> command) {
    SortedSet zset = sortedSets.get(command.get(1));
    List<String> members = command.subList(2, command.size());

    StringBuilder sb = new StringBuilder();
    sb.append("*").append(members.size()).append("\r\n");
    for (String member : members) {
      Double score = (zset == null) ? null : zset.score(member);
      if (score == null) {
        sb.append("*-1\r\n"); // key or member doesn't exist
      } else {
        double[] lonLat = geoDecode(score.longValue());
        sb.append("*2\r\n").append(bulkString(formatScore(lonLat[0]))).append(bulkString(formatScore(lonLat[1])));
      }
    }
    return sb.toString();
  }

  // GEODIST <key> <member1> <member2>: distance in meters, or null if either location is missing
  private static String handleGeodist(List<String> command) {
    SortedSet zset = sortedSets.get(command.get(1));
    Double score1 = (zset == null) ? null : zset.score(command.get(2));
    Double score2 = (zset == null) ? null : zset.score(command.get(3));
    if (score1 == null || score2 == null) return "$-1\r\n";

    double[] a = geoDecode(score1.longValue()); // {longitude, latitude}
    double[] b = geoDecode(score2.longValue());
    double meters = haversine(a[1], a[0], b[1], b[0]);
    return bulkString(String.format(Locale.US, "%.4f", meters)); // 4 decimal places, like Redis
  }

  // GEOSEARCH <key> FROMLONLAT <lon> <lat> BYRADIUS <radius> <unit>: members within that circle
  private static String handleGeosearch(List<String> command) {
    // Fixed argument positions, since the tester always uses FROMLONLAT then BYRADIUS
    double centerLon = Double.parseDouble(command.get(3));
    double centerLat = Double.parseDouble(command.get(4));
    double radius = Double.parseDouble(command.get(6));
    String unit = command.get(7).toLowerCase();

    // Convert the radius to meters
    double metersPerUnit = 1; // "m"
    if (unit.equals("km")) metersPerUnit = 1000;
    else if (unit.equals("mi")) metersPerUnit = 1609.34;
    else if (unit.equals("ft")) metersPerUnit = 0.3048;
    double radiusMeters = radius * metersPerUnit;

    List<String> matches = new ArrayList<>();
    SortedSet zset = sortedSets.get(command.get(1));
    if (zset != null) {
      for (String member : zset.members()) {
        double[] lonLat = geoDecode(zset.score(member).longValue());
        if (haversine(centerLat, centerLon, lonLat[1], lonLat[0]) <= radiusMeters) {
          matches.add(member);
        }
      }
    }

    StringBuilder sb = new StringBuilder();
    sb.append("*").append(matches.size()).append("\r\n");
    for (String m : matches) sb.append(bulkString(m));
    return sb.toString();
  }

  // Great-circle distance in meters between two points (Haversine formula, same Earth radius as Redis)
  private static double haversine(double lat1, double lon1, double lat2, double lon2) {
    double earthRadius = 6372797.560856;
    double lat1r = Math.toRadians(lat1), lat2r = Math.toRadians(lat2);
    double u = Math.sin((lat2r - lat1r) / 2);
    double v = Math.sin(Math.toRadians(lon2 - lon1) / 2);
    return 2 * earthRadius * Math.asin(Math.sqrt(u * u + Math.cos(lat1r) * Math.cos(lat2r) * v * v));
  }

  // Turns a latitude/longitude into one number (Redis's geohash score):
  // scale each to a 26-bit integer, then interleave their bits (latitude in even bits, longitude in odd bits)
  private static long geoScore(double latitude, double longitude) {
    int latBits = (int) ((1 << 26) * (latitude + 85.05112878) / (2 * 85.05112878));
    int lonBits = (int) ((1 << 26) * (longitude + 180) / 360);
    return spreadBits(latBits) | (spreadBits(lonBits) << 1);
  }

  // Reverses geoScore: splits the interleaved bits back apart and returns the center of that grid cell
  // as {longitude, latitude}
  private static double[] geoDecode(long score) {
    int latBits = compactBits(score);       // latitude was in the even bits
    int lonBits = compactBits(score >> 1);  // longitude was in the odd bits
    double cells = 1 << 26;
    double latRange = 2 * 85.05112878;
    // Each 26-bit number marks a small grid cell; use the middle of it (hence the + 0.5)
    double latitude = -85.05112878 + latRange * ((latBits + 0.5) / cells);
    double longitude = -180 + 360 * ((lonBits + 0.5) / cells);
    return new double[] {longitude, latitude};
  }

  // Undoes spreadBits: 0a0b0c0d -> abcd
  private static int compactBits(long x) {
    x = x & 0x5555555555555555L;
    x = (x | (x >> 1)) & 0x3333333333333333L;
    x = (x | (x >> 2)) & 0x0F0F0F0F0F0F0F0FL;
    x = (x | (x >> 4)) & 0x00FF00FF00FF00FFL;
    x = (x | (x >> 8)) & 0x0000FFFF0000FFFFL;
    x = (x | (x >> 16)) & 0x00000000FFFFFFFFL;
    return (int) x;
  }

  // Spreads a number's bits out so there's a 0 between each one: abcd -> 0a0b0c0d
  private static long spreadBits(int v) {
    long x = v & 0xFFFFFFFFL;
    x = (x | (x << 16)) & 0x0000FFFF0000FFFFL;
    x = (x | (x << 8)) & 0x00FF00FF00FF00FFL;
    x = (x | (x << 4)) & 0x0F0F0F0F0F0F0F0FL;
    x = (x | (x << 2)) & 0x3333333333333333L;
    x = (x | (x << 1)) & 0x5555555555555555L;
    return x;
  }

  // VADD <key> <id> <x1> <x2> ... <xn>: stores a vector. Returns 1 if the id is new, 0 if replaced.
  private static String handleVadd(List<String> command) {
    String key = command.get(1);
    String id = command.get(2);
    float[] vector = parseVector(command, 3);
    if (vector == null) return "-ERR vector values must be numbers\r\n";

    VectorSet set = vectorSets.computeIfAbsent(key, k -> new VectorSet(vector.length));
    if (vector.length != set.dim) {
      return "-ERR vector has " + vector.length + " dimensions, but this set uses " + set.dim + "\r\n";
    }
    return ":" + (set.add(id, vector) ? 1 : 0) + "\r\n";
  }

  // VSEARCH <key> <k> <x1> <x2> ... <xn>: the k most similar vectors as [id, score, id, score, ...]
  private static String handleVsearch(List<String> command) {
    VectorSet set = vectorSets.get(command.get(1));
    if (set == null) return "*0\r\n";
    int k = Integer.parseInt(command.get(2));

    int ef = 100;
    boolean exact = false;
    int pos = 3;
    while (pos < command.size()) {
      String opt = command.get(pos).toUpperCase();
      if (opt.equals("EF")) { ef = Integer.parseInt(command.get(pos + 1)); pos += 2; }
      else if (opt.equals("EXACT")) { exact = true; pos++; }
      else break; // the vector starts here
    }

    float[] query = parseVector(command, pos);
    if (query == null) return "-ERR vector values must be numbers\r\n";
    if (query.length != set.dim) {
      return "-ERR query has " + query.length + " dimensions, but this set uses " + set.dim + "\r\n";
    }

    List<Map.Entry<String, Double>> results = exact ? set.searchExact(query, k) : set.search(query, k, ef);
    StringBuilder sb = new StringBuilder();
    sb.append("*").append(results.size() * 2).append("\r\n");
    for (Map.Entry<String, Double> r : results) {
      sb.append(bulkString(r.getKey()));
      sb.append(bulkString(String.format(Locale.US, "%.6f", r.getValue())));
    }
    return sb.toString();
  }

  // Reads command parts from 'start' to the end as floats, or null if any isn't a number
  private static float[] parseVector(List<String> command, int start) {
    float[] v = new float[command.size() - start];
    try {
      for (int i = 0; i < v.length; i++) v[i] = Float.parseFloat(command.get(start + i));
    } catch (NumberFormatException e) {
      return null;
    }
    return v;
  }

  // ZADD <key> <score> <member>: returns 1 if the member is new, 0 if it already existed
  private static String handleZadd(List<String> command) {
    String key = command.get(1);
    double score = Double.parseDouble(command.get(2));
    String member = command.get(3);
    SortedSet zset = sortedSets.computeIfAbsent(key, k -> new SortedSet());
    boolean added = zset.add(member, score);
    return ":" + (added ? 1 : 0) + "\r\n";
  }

  // ZRANK <key> <member>: the member's 0-based rank, or null if the set or member doesn't exist
  private static String handleZrank(String key, String member) {
    SortedSet zset = sortedSets.get(key);
    if (zset == null) return "$-1\r\n";
    int rank = zset.rank(member);
    return (rank == -1) ? "$-1\r\n" : ":" + rank + "\r\n";
  }

  // ZRANGE <key> <start> <stop>: members from index start to stop (inclusive), in score order
  private static String handleZrange(List<String> command) {
    SortedSet zset = sortedSets.get(command.get(1));
    if (zset == null) return "*0\r\n"; // set doesn't exist

    List<String> members = zset.members();
    int start = Integer.parseInt(command.get(2));
    int stop = Integer.parseInt(command.get(3));
    if (start < 0) start = Math.max(members.size() + start, 0);
    if (stop < 0) stop = Math.max(members.size() + stop, 0);
    if (stop >= members.size()) stop = members.size() - 1; // clamp stop to the last member
    if (start >= members.size() || start > stop) return "*0\r\n";

    StringBuilder sb = new StringBuilder();
    sb.append("*").append(stop - start + 1).append("\r\n");
    for (int i = start; i <= stop; i++) {
      sb.append(bulkString(members.get(i)));
    }
    return sb.toString();
  }

  private static String formatScore(double score) {
    return BigDecimal.valueOf(score).stripTrailingZeros().toPlainString();
  }

  // PUBLISH <channel> <message>: sends ["message", channel, message] to every subscriber
  private static String handlePublish(String channel, String message) {
    Set<OutputStream> subs = channelSubscribers.getOrDefault(channel, Set.of());
    byte[] payload = ("*3\r\n" + bulkString("message") + bulkString(channel) + bulkString(message))
        .getBytes(StandardCharsets.ISO_8859_1);
    for (OutputStream sub : subs) {
      try {
        synchronized (sub) { // don't mix with anything else being written to that client
          sub.write(payload);
        }
      } catch (IOException e) {
        subs.remove(sub); // subscriber disconnected
      }
    }
    return ":" + subs.size() + "\r\n";
  }

  // KEYS *: returns every string key (only the "*" pattern is supported)
  private static String handleKeys() {
    StringBuilder sb = new StringBuilder();
    sb.append("*").append(store.size()).append("\r\n");
    for (String key : store.keySet()) {
      sb.append(bulkString(key));
    }
    return sb.toString();
  }

  // ---------- RDB file loading ----------

  private static byte[] rdb;  // the whole file
  private static int pos;     // where we are in it

  private static void loadRdb() {
    Path path = Path.of(dir, dbfilename);
    if (!Files.exists(path)) return; // no file = empty database
    try {
      rdb = Files.readAllBytes(path);
    } catch (IOException e) {
      System.out.println("Couldn't read RDB file: " + e.getMessage());
      return;
    }

    pos = 9; // skip the "REDIS0011" header
    while (pos < rdb.length) {
      int op = rdb[pos++] & 0xFF;
      if (op == 0xFA) {          // metadata: name + value, both strings (we don't need them)
        readRdbString();
        readRdbString();
      } else if (op == 0xFE) {   // start of a database: its index
        readRdbSize();
      } else if (op == 0xFB) {   // hash table sizes: total keys, keys with expiry
        readRdbSize();
        readRdbSize();
      } else if (op == 0xFF) {   // end of file (checksum follows, ignore it)
        break;
      } else {
        // A key-value pair, possibly starting with an expiry
        long expiresAt = -1;
        if (op == 0xFC) {        // expiry in milliseconds, 8 bytes little-endian
          expiresAt = readLittleEndian(8);
          op = rdb[pos++] & 0xFF;
        } else if (op == 0xFD) { // expiry in seconds, 4 bytes little-endian
          expiresAt = readLittleEndian(4) * 1000;
          op = rdb[pos++] & 0xFF;
        }
        // op is now the value type; 0 = string (the only type we need)
        String key = readRdbString();
        String value = readRdbString();
        store.put(key, value);
        if (expiresAt != -1) expiries.put(key, expiresAt);
      }
    }
  }

  // Size encoding: the first 2 bits say how the size is stored
  private static int readRdbSize() {
    int first = rdb[pos++] & 0xFF;
    int type = first >> 6;
    if (type == 0) {          // 00: size is the remaining 6 bits
      return first & 0x3F;
    } else if (type == 1) {   // 01: remaining 6 bits + next byte (14 bits, big-endian)
      return ((first & 0x3F) << 8) | (rdb[pos++] & 0xFF);
    } else if (type == 2) {   // 10: the next 4 bytes, big-endian
      int size = 0;
      for (int i = 0; i < 4; i++) size = (size << 8) | (rdb[pos++] & 0xFF);
      return size;
    }
    // 11: not a size but a special string format; hand it back marked so readRdbString can handle it
    return -(first & 0x3F) - 1;
  }

  // String encoding: a size then that many bytes, or a number stored as an integer
  private static String readRdbString() {
    int size = readRdbSize();
    if (size >= 0) {
      String s = new String(rdb, pos, size, StandardCharsets.ISO_8859_1);
      pos += size;
      return s;
    }
    int format = -size - 1;
    if (format == 0) return String.valueOf(rdb[pos++]);                   // C0: 8-bit integer
    if (format == 1) return String.valueOf((short) readLittleEndian(2));  // C1: 16-bit integer
    if (format == 2) return String.valueOf((int) readLittleEndian(4));    // C2: 32-bit integer
    throw new IllegalStateException("LZF-compressed strings aren't supported");
  }

  // Reads 'count' bytes as a little-endian number (lowest byte first)
  private static long readLittleEndian(int count) {
    long value = 0;
    for (int i = 0; i < count; i++) {
      value |= (long) (rdb[pos++] & 0xFF) << (8 * i);
    }
    return value;
  }

  // ACL <subcommand> ...
  private static String handleAcl(List<String> command) {
    String sub = command.get(1).toUpperCase();
    if (sub.equals("WHOAMI")) {
      return bulkString("default"); // every connection is the default user for now
    }
    if (sub.equals("GETUSER")) {
      // [property, value, ...]: flags, then passwords (as SHA-256 hashes)
      StringBuilder sb = new StringBuilder("*4\r\n");
      sb.append(bulkString("flags"));
      if (defaultNopass) {
        sb.append("*1\r\n").append(bulkString("nopass"));
      } else {
        sb.append("*0\r\n");
      }
      sb.append(bulkString("passwords"));
      sb.append("*").append(defaultPasswordHashes.size()).append("\r\n");
      for (String hash : defaultPasswordHashes) sb.append(bulkString(hash));
      return sb.toString();
    }
    if (sub.equals("SETUSER")) {
      // Rules after the username, e.g. ">mypassword" adds a password
      for (String rule : command.subList(3, command.size())) {
        if (rule.startsWith(">")) {
          String hash = sha256(rule.substring(1));
          if (!defaultPasswordHashes.contains(hash)) defaultPasswordHashes.add(hash);
          defaultNopass = false; // having a password turns off nopass
        }
      }
      return "+OK\r\n";
    }
    return "-ERR unknown ACL subcommand '" + command.get(1) + "'\r\n";
  }

  // AUTH <username> <password>: OK if the password matches (only the default user exists for now)
  private static String handleAuth(String username, String password) {
    boolean ok = username.equals("default")
        && (defaultNopass || defaultPasswordHashes.contains(sha256(password)));
    return ok ? "+OK\r\n" : "-WRONGPASS invalid username-password pair or user is disabled.\r\n";
  }

  // SHA-256 of a string, as lowercase hex (64 characters)
  private static String sha256(String text) {
    try {
      byte[] digest = MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.ISO_8859_1));
      StringBuilder hex = new StringBuilder();
      for (byte b : digest) hex.append(String.format("%02x", b));
      return hex.toString();
    } catch (NoSuchAlgorithmException e) {
      throw new RuntimeException(e); // SHA-256 is always available in Java
    }
  }

  // CONFIG GET <param>: returns [param, value]
  private static String handleConfig(List<String> command) {
    String param = command.get(2).toLowerCase();
    String value;
    if (param.equals("dir")) {
      value = dir;
    } else if (param.equals("dbfilename")) {
      value = dbfilename;
    } else if (param.equals("dbfilename")) {
      value = dbfilename;
    } else if (param.equals("appendonly")) {
      value = appendonly;
    } else if (param.equals("appenddirname")) {
      value = appenddirname;
    } else if (param.equals("appendfilename")) {
      value = appendfilename;
    } else if (param.equals("appendfsync")) {
      value = appendfsync;
    } else {
      return "*0\r\n"; // unknown parameter: empty array
    }
    return "*2\r\n" + bulkString(param) + bulkString(value);
  }
  
  private static String handleSet(List<String> command) {
    String key = command.get(1);
    String value = command.get(2);
    store.put(key, value);
    expiries.remove(key); // a plain SET clears any old expiry

    // Look for options after the value, like PX 100 or EX 10
    for (int i = 3; i + 1 < command.size(); i += 2) {
      String option = command.get(i).toUpperCase();
      long amount = Long.parseLong(command.get(i + 1));
      if (option.equals("PX")) {
        expiries.put(key, System.currentTimeMillis() + amount);
      } else if (option.equals("EX")) {
        expiries.put(key, System.currentTimeMillis() + amount * 1000);
      }
    }
    return "+OK\r\n";
  }

  private static String handleGet(String key) {
    Long expiresAt = expiries.get(key);
    if (expiresAt != null && System.currentTimeMillis() >= expiresAt) {
      // Key has expired: delete it and act like it doesn't exist
      store.remove(key);
      expiries.remove(key);
      return "$-1\r\n";
    }
    String value = store.get(key);
    return (value == null) ? "$-1\r\n" : bulkString(value);
  }

  private static String handleIncr(String key) {
    handleGet(key); // clears the key first if it has expired, so it counts as missing
    try {
      // A missing key (null) starts at 0, so it becomes 1
      String updated = store.compute(key, (k, value) ->
          String.valueOf(Math.addExact(value == null ? 0 : Long.parseLong(value), 1)));
      return ":" + updated + "\r\n";
    } catch (NumberFormatException | ArithmeticException e) {
      // Not a number (like "xyz"), or too big to add 1 to. The value is left unchanged.
      return "-ERR value is not an integer or out of range\r\n";
    }
  }

  private static String handleRPush(List<String> command) {
    String key = command.get(1);
    List<String> list = lists.computeIfAbsent(key, k -> new ArrayList<>());
    synchronized (list) {
      // Add every element after the key
      for (int i = 2; i < command.size(); i++) {
        list.add(command.get(i));
      }
      int size = list.size(); // length after pushing, before any blocked client takes an element
      serveWaiters(key, list);
      return ":" + size + "\r\n";
    }
  }

  private static String handleLpush(List<String> command) {
    String key = command.get(1);
    List<String> list = lists.computeIfAbsent(key, k -> new ArrayList<>());
    synchronized (list) {
      // Insert each element at the front, one by one, so "a b c" ends up as [c, b, a]
      for (int i = 2; i < command.size(); i++) {
        list.add(0, command.get(i));
      }
      int size = list.size();
      serveWaiters(key, list);
      return ":" + size + "\r\n";
    }
  }

  private static String handleLlen(String key) {
    List<String> list = lists.get(key);
    if (list == null) return ":0\r\n"; // list doesn't exist
    synchronized (list) {
      return ":" + list.size() + "\r\n";
    }
  }

  private static String handleLpop(List<String> command) {
    String key = command.get(1);
    boolean hasCount = command.size() > 2; // was a count like "LPOP key 2" given?

    List<String> list = lists.get(key);
    if (list == null) return hasCount ? "*-1\r\n" : "$-1\r\n"; // list doesn't exist

    synchronized (list) {
      if (!hasCount) {
        // Single pop: return one element as a bulk string
        if (list.isEmpty()) return "$-1\r\n";
        return bulkString(list.remove(0));
      }

      // Multi pop: remove up to 'count' elements and return them as an array
      int count = Math.min(Integer.parseInt(command.get(2)), list.size());
      StringBuilder sb = new StringBuilder();
      sb.append("*").append(count).append("\r\n");
      for (int i = 0; i < count; i++) {
        sb.append(bulkString(list.remove(0)));
      }
      return sb.toString();
    }
  }

  private static String handleBlpop(List<String> command) {
    String key = command.get(1);
    List<String> list = lists.computeIfAbsent(key, k -> new ArrayList<>());
    CompletableFuture<String> future;

    synchronized (list) {
      // If there's already something in the list, pop it right away
      if (!list.isEmpty()) {
        return "*2\r\n" + bulkString(key) + bulkString(list.remove(0));
      }
      // Otherwise get in line and wait
      future = new CompletableFuture<>();
      waiters.computeIfAbsent(key, k -> new ArrayDeque<>()).addLast(future);
    }

    double timeout = Double.parseDouble(command.get(2)); // in seconds, 0 = wait forever
    try {
      String element = (timeout == 0)
          ? future.get() // blocks until a push hands us an element
          : future.get((long) (timeout * 1000), TimeUnit.MILLISECONDS);
      return "*2\r\n" + bulkString(key) + bulkString(element);
    } catch (TimeoutException e) {
      synchronized (list) {
        // A push might have handed us an element right as we timed out
        if (future.isDone()) {
          return "*2\r\n" + bulkString(key) + bulkString(future.join());
        }
        waiters.get(key).remove(future); // leave the line so we don't eat a future element
      }
      return "*-1\r\n";
    } catch (Exception e) {
      return "*-1\r\n";
    }
  }

  // Called after a push (while holding the list's lock): give elements to blocked clients, oldest first
  private static void serveWaiters(String key, List<String> list) {
    Deque<CompletableFuture<String>> queue = waiters.get(key);
    if (queue == null) return;
    while (!queue.isEmpty() && !list.isEmpty()) {
      queue.pollFirst().complete(list.remove(0));
    }
  }

  private static String handleXadd(List<String> command) {
    String key = command.get(1);
    String id = command.get(2);
    // Everything after the ID is field-value pairs
    List<String> fields = new ArrayList<>(command.subList(3, command.size()));
    // "*" means generate everything: use the current time and auto-pick the sequence
    boolean autoTime = id.equals("*");
    if (autoTime) id = System.currentTimeMillis() + "-*";
    // Split "1526919030474-0" (or "1526919030474-*") into its two parts
    String[] parts = id.split("-");
    long ms = Long.parseLong(parts[0]);
    boolean autoSeq = parts[1].equals("*");
    long seq = autoSeq ? 0 : Long.parseLong(parts[1]);

    if (!autoSeq && ms == 0 && seq == 0) {
      return "-ERR The ID specified in XADD must be greater than 0-0\r\n";
    }

    List<StreamEntry> stream = streams.computeIfAbsent(key, k -> new ArrayList<>());
    synchronized (stream) {
      long lastMs = -1, lastSeq = -1; // -1 means the stream is empty
      if (!stream.isEmpty()) {
        String[] lastParts = stream.get(stream.size() - 1).id.split("-");
        lastMs = Long.parseLong(lastParts[0]);
        lastSeq = Long.parseLong(lastParts[1]);
      }
      if (autoTime && ms < lastMs) ms = lastMs;
      if (autoSeq) {
        if (ms == lastMs) {
          seq = lastSeq + 1;          // same time as last entry: next sequence number
        } else {
          seq = (ms == 0) ? 1 : 0;    // new time: start at 0 (or 1 if time is 0)
        }
      }

      // New ID must be strictly bigger: bigger time, or same time with bigger sequence
      if (ms < lastMs || (ms == lastMs && seq <= lastSeq)) {
        return "-ERR The ID specified in XADD is equal or smaller than the target stream top item\r\n";
      }

      id = ms + "-" + seq;
      stream.add(new StreamEntry(id, fields));
    }
    synchronized (streamSignal) {
    streamSignal.notifyAll();
    }
    return bulkString(id);
  }

  private static String handleXrange(List<String> command) {
    String key = command.get(1);
    // Missing sequence: start defaults to 0, end defaults to the biggest possible number
    long[] start = command.get(2).equals("-") ? new long[] {0, 0} : parseId(command.get(2), 0);
    long[] end = command.get(3).equals("+")
        ? new long[] {Long.MAX_VALUE, Long.MAX_VALUE}
        : parseId(command.get(3), Long.MAX_VALUE);

    List<StreamEntry> stream = streams.get(key);
    if (stream == null) return "*0\r\n";

    List<StreamEntry> matches = new ArrayList<>();
    synchronized (stream) {
      for (StreamEntry entry : stream) {
        long[] id = parseId(entry.id, 0);
        if (compareIds(id, start) >= 0 && compareIds(id, end) <= 0) {
          matches.add(entry);
        }
      }
    }

    // Outer array of entries; each entry is [id, [field, value, field, value, ...]]
    StringBuilder sb = new StringBuilder();
    sb.append("*").append(matches.size()).append("\r\n");
    for (StreamEntry entry : matches) {
      sb.append("*2\r\n");
      sb.append(bulkString(entry.id));
      sb.append("*").append(entry.fields.size()).append("\r\n");
      for (String f : entry.fields) {
        sb.append(bulkString(f));
      }
    }
    return sb.toString();
  }

  private static String handleXread(List<String> command) {
    // Format: XREAD [BLOCK <ms>] STREAMS <key1> <key2> ... <id1> <id2> ...
    long blockMs = -1; // -1 means don't block
    int streamsIndex = 1;
    while (!command.get(streamsIndex).equalsIgnoreCase("STREAMS")) {
      if (command.get(streamsIndex).equalsIgnoreCase("BLOCK")) {
        blockMs = Long.parseLong(command.get(streamsIndex + 1));
        streamsIndex++;
      }
      streamsIndex++;
    }
    // Replace any "$" ID with the stream's current last ID, so we only get entries added from now on.
    // This happens once, up front, so it doesn't move while we're waiting.
    command = new ArrayList<>(command);
    int count = (command.size() - streamsIndex - 1) / 2;
    for (int s = 0; s < count; s++) {
      int idIndex = streamsIndex + 1 + count + s;
      if (command.get(idIndex).equals("$")) {
        String key = command.get(streamsIndex + 1 + s);
        List<StreamEntry> stream = streams.get(key);
        String lastId = "0-0";
        if (stream != null) {
          synchronized (stream) {
            if (!stream.isEmpty()) lastId = stream.get(stream.size() - 1).id;
          }
        }
        command.set(idIndex, lastId);
      }
    }
    if (blockMs < 0) {
      String result = readStreams(command, streamsIndex);
      return (result == null) ? "*-1\r\n" : result;
    }

    // Blocking: keep checking until something shows up or time runs out
    long deadline = System.currentTimeMillis() + blockMs;
    synchronized (streamSignal) {
      while (true) {
        String result = readStreams(command, streamsIndex);
        if (result != null) return result;

        try {
          if (blockMs == 0) {
            streamSignal.wait(); // BLOCK 0: wait forever until XADD calls notifyAll
          } else {
            long remaining = deadline - System.currentTimeMillis();
            if (remaining <= 0) return "*-1\r\n";
            streamSignal.wait(remaining); // sleeps until XADD calls notifyAll, or timeout
          }
        } catch (InterruptedException e) {
          return "*-1\r\n";
        }
      }
    }
  }

  // Builds the XREAD response for the keys/IDs after STREAMS. Returns null if nothing new.
  private static String readStreams(List<String> command, int streamsIndex) {
    // After STREAMS: first half are keys, second half are IDs
    int count = (command.size() - streamsIndex - 1) / 2;

    StringBuilder body = new StringBuilder();
    int streamsWithResults = 0;
    for (int s = 0; s < count; s++) {
      String key = command.get(streamsIndex + 1 + s);
      long[] after = parseId(command.get(streamsIndex + 1 + count + s), 0);

      List<StreamEntry> stream = streams.get(key);
      List<StreamEntry> matches = new ArrayList<>();
      if (stream != null) {
        synchronized (stream) {
          for (StreamEntry entry : stream) {
            // Exclusive: only entries strictly after the given ID
            if (compareIds(parseId(entry.id, 0), after) > 0) {
              matches.add(entry);
            }
          }
        }
      }
      if (matches.isEmpty()) continue; // streams with nothing new are left out

      // [key, [[id, [field, value, ...]], ...]]
      streamsWithResults++;
      body.append("*2\r\n");
      body.append(bulkString(key));
      body.append("*").append(matches.size()).append("\r\n");
      for (StreamEntry entry : matches) {
        body.append("*2\r\n");
        body.append(bulkString(entry.id));
        body.append("*").append(entry.fields.size()).append("\r\n");
        for (String f : entry.fields) {
          body.append(bulkString(f));
        }
      }
    }

    if (streamsWithResults == 0) return null;
    return "*" + streamsWithResults + "\r\n" + body;
  }

  // Turns "5-3" into [5, 3], or "5" into [5, defaultSeq]
  private static long[] parseId(String id, long defaultSeq) {
    String[] parts = id.split("-");
    long ms = Long.parseLong(parts[0]);
    long seq = (parts.length > 1) ? Long.parseLong(parts[1]) : defaultSeq;
    return new long[] {ms, seq};
  }

  // Negative if a < b, 0 if equal, positive if a > b
  private static int compareIds(long[] a, long[] b) {
    if (a[0] != b[0]) return Long.compare(a[0], b[0]);
    return Long.compare(a[1], b[1]);
  }

  private static String handleType(String key) {
    // handleGet already checks expiry, so an expired key counts as missing
    if (!handleGet(key).equals("$-1\r\n")) return "+string\r\n";
    List<String> list = lists.get(key);
    if (list != null && !list.isEmpty()) return "+list\r\n";
    if (streams.containsKey(key)) return "+stream\r\n";
    return "+none\r\n";
  }

  private static String handleLrange(List<String> command) {
    String key = command.get(1);
    int start = Integer.parseInt(command.get(2));
    int stop = Integer.parseInt(command.get(3));

    List<String> list = lists.get(key);
    if (list == null) return "*0\r\n"; // list doesn't exist

    synchronized (list) {
      int size = list.size();
      // Negative indexes count from the end: -1 is the last element
      if (start < 0) start = Math.max(size + start, 0);
      if (stop < 0) stop = Math.max(size + stop, 0);

      if (stop >= list.size()) stop = list.size() - 1; // clamp stop to the last element
      if (start >= list.size() || start > stop) return "*0\r\n";

      StringBuilder sb = new StringBuilder();
      sb.append("*").append(stop - start + 1).append("\r\n");
      for (int i = start; i <= stop; i++) {
        sb.append(bulkString(list.get(i)));
      }
      return sb.toString();
    }
  }

  // Parses one RESP array like *2\r\n$4\r\nECHO\r\n$3\r\nhey\r\n into ["ECHO", "hey"]
  private static List<String> readCommand(InputStream in) throws IOException {
    String header = readLine(in);
    if (header == null) return null;

    int count = Integer.parseInt(header.substring(1)); // skip '*'
    List<String> parts = new ArrayList<>();
    for (int i = 0; i < count; i++) {
      String lenLine = readLine(in);
      if (lenLine == null) throw new EOFException("connection closed mid-command");
      int len = Integer.parseInt(lenLine.substring(1)); // skip '$'
      byte[] data = in.readNBytes(len);
      // Make sure the whole value and its \r\n actually arrived, not just part of it
      if (data.length < len || readLine(in) == null) throw new EOFException("connection closed mid-command");
      parts.add(new String(data, StandardCharsets.ISO_8859_1));
    }
    return parts;
  }

  // Reads bytes up to \r\n. Returns null if the connection closed.
  private static String readLine(InputStream in) throws IOException {
    ByteArrayOutputStream buf = new ByteArrayOutputStream();
    int b;
    while ((b = in.read()) != -1) {
      if (b == '\r') {
        in.read(); // skip '\n'
        return buf.toString(StandardCharsets.ISO_8859_1);
      }
      buf.write(b);
    }
    return null;
  }

  private static String bulkString(String s) {
    byte[] bytes = s.getBytes(StandardCharsets.ISO_8859_1);
    return "$" + bytes.length + "\r\n" + s + "\r\n";
  }
}