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
  private static final Set<String> WRITE_COMMANDS = Set.of("SET", "INCR", "RPUSH", "LPUSH", "LPOP", "XADD");
  private static final String EMPTY_RDB_BASE64 =
      "UkVESVMwMDEx+glyZWRpcy12ZXIFNy4yLjD6CnJlZGlzLWJpdHPAQPoFY3RpbWXCbQi8ZfoIdXNlZC1tZW3CsMQQAPoIYW9mLWJhc2XAAP/wbjv+wP9aog==";
  private static String masterHost = null;
  private static int masterPort = 0;
  // Connection to the master, kept open for the rest of the handshake in later stages
  private static Socket masterSocket = null;
  private static long replicaOffset = 0;

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
      } else if (args[i].equals("--replicaof")) {
        role = "slave";
        // e.g. --replicaof "localhost 6379" -> host "localhost", port 6379
        String[] parts = args[i + 1].split(" ");
        masterHost = parts[0];
        masterPort = Integer.parseInt(parts[1]);
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
  
  // Sends a command to every replica as a RESP array (no reply is expected)
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
      int bytes = part.getBytes(StandardCharsets.UTF_8).length;
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
    out.write(sb.toString().getBytes(StandardCharsets.UTF_8));
    out.flush();
  }

  private static void handleClient(Socket clientSocket) {
    try (clientSocket) {
      InputStream in = new BufferedInputStream(clientSocket.getInputStream());
      OutputStream out = clientSocket.getOutputStream();
      boolean inMulti = false; // is this connection inside a MULTI transaction?
      List<List<String>> queued = new ArrayList<>(); // commands saved up during MULTI
      Map<String, Long> watchedKeys = new HashMap<>(); // watched key -> its version at WATCH time

      while (true) {
        List<String> command = readCommand(in);
        if (command == null) break; // client disconnected

        String name = command.get(0).toUpperCase();
        String response;

        if (name.equals("MULTI")) {
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
              .getBytes(StandardCharsets.UTF_8));
          byte[] rdb = Base64.getDecoder().decode(EMPTY_RDB_BASE64);
          out.write(("$" + rdb.length + "\r\n").getBytes(StandardCharsets.UTF_8));
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

        out.write(response.getBytes(StandardCharsets.UTF_8));
      }
    } catch (IOException e) {
      System.out.println("IOException: " + e.getMessage());
    }
  }

  // Runs a single command and returns its RESP response
  private static String executeCommand(List<String> command) {
    String name = command.get(0).toUpperCase();
    switch (name) {
      case "SET": case "INCR": case "RPUSH": case "LPUSH": case "LPOP": case "BLPOP": case "XADD":
        keyVersions.merge(command.get(1), 1L, Long::sum);
    }
    if (WRITE_COMMANDS.contains(name)) {
      propagate(command);
    }
    switch (name) {
      case "PING":   return "+PONG\r\n";
      case "REPLCONF": return "+OK\r\n";
      case "WAIT":   return handleWait(command);
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
      int len = Integer.parseInt(lenLine.substring(1)); // skip '$'
      byte[] data = in.readNBytes(len);
      readLine(in); // consume the \r\n after the data
      parts.add(new String(data, StandardCharsets.UTF_8));
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
        return buf.toString(StandardCharsets.UTF_8);
      }
      buf.write(b);
    }
    return null;
  }

  private static String bulkString(String s) {
    byte[] bytes = s.getBytes(StandardCharsets.UTF_8);
    return "$" + bytes.length + "\r\n" + s + "\r\n";
  }
}