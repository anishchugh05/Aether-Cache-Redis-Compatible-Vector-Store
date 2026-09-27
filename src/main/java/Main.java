import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;

public class Main {
  public static void main(String[] args){
    System.out.println("Logs from your program will appear here!");

    int port = 6379;
    try (ServerSocket serverSocket = new ServerSocket(port)) {
      serverSocket.setReuseAddress(true);

      while (true) {
        Socket clientSocket = serverSocket.accept();
        new Thread(() -> handleClient(clientSocket)).start();
      }
    } catch (IOException e) {
      System.out.println("IOException: " + e.getMessage());
    }
  }

  private static void handleClient(Socket clientSocket) {
    try (clientSocket) {
      InputStream in = new BufferedInputStream(clientSocket.getInputStream());
      OutputStream out = clientSocket.getOutputStream();

      while (true) {
        List<String> command = readCommand(in);
        if (command == null) break; // client disconnected

        String name = command.get(0).toUpperCase();
        String response;
        switch (name) {
          case "PING":
            response = "+PONG\r\n";
            break;
          case "ECHO":
            response = bulkString(command.get(1));
            break;
          default:
            response = "-ERR unknown command '" + command.get(0) + "'\r\n";
        }
        out.write(response.getBytes(StandardCharsets.UTF_8));
      }
    } catch (IOException e) {
      System.out.println("IOException: " + e.getMessage());
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