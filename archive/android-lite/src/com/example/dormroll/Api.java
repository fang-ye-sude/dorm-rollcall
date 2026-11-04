package com.example.dormroll;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.Locale;

import javax.net.ssl.SNIHostName;
import javax.net.ssl.SSLParameters;
import javax.net.ssl.SSLSocket;
import javax.net.ssl.SSLSocketFactory;

/**
 * 与 iPad 客户端同一套接口。
 *
 * 注意：服务器的云盾会重置带 ALPN 的 TLS 连接（HttpURLConnection 默认发 h2），
 * 所以这里用裸 TLS（不声明 ALPN）+ 手工 HTTP/1.1，实测可通。
 */
public class Api {

    /** 传输实现：默认裸 TLS；MainActivity 会注入 WebView 桥（Chromium 栈，能过云盾） */
    public interface Transport {
        Resp call(String method, String path, String token, String body, int timeoutMs) throws Exception;
    }

    public static volatile Transport transport = null;

    public static final String BASE = "https://example.com/dianming/api";
    private static final String HOST = "example.com";
    private static final int PORT = 443;

    public static class Resp {
        public final int code;
        public final JSONObject json;

        public Resp(int code, JSONObject json) {
            this.code = code;
            this.json = json;
        }

        public boolean ok() {
            return json != null && json.optBoolean("ok", false);
        }
    }

    /** 裸 TLS 请求：不协商 ALPN，手工写 HTTP/1.1，读到底再解析 */
    private static Resp raw(String method, String path, String token, String body, int timeoutMs)
            throws Exception {
        if (transport != null) {
            return transport.call(method, path, token, body, timeoutMs);
        }
        return rawTls(method, path, token, body, timeoutMs);
    }

    private static Resp rawTls(String method, String path, String token, String body, int timeoutMs)
            throws IOException {
        try {
            return raw0(method, path, token, body, timeoutMs);
        } catch (Exception e) {
            android.util.Log.e("dianming", "请求失败 " + method + " " + path, e);
            if (e instanceof IOException) throw (IOException) e;
            throw new IOException(e.getClass().getSimpleName() + ": " + e.getMessage());
        }
    }

    private static Resp raw0(String method, String path, String token, String body, int timeoutMs)
            throws IOException {
        Socket plain = new Socket();
        SSLSocket s = null;
        try {
            plain.connect(new InetSocketAddress(HOST, PORT), 8000);
            plain.setSoTimeout(timeoutMs);
            s = (SSLSocket) ((SSLSocketFactory) SSLSocketFactory.getDefault())
                    .createSocket(plain, HOST, PORT, true);
            SSLParameters p = s.getSSLParameters();
            p.setServerNames(java.util.Collections.singletonList(new SNIHostName(HOST)));
            p.setEndpointIdentificationAlgorithm("HTTPS");   // 证书必须对得上 example.com
            s.setSSLParameters(p);          // 不设置 applicationProtocols → 不发 ALPN
            s.setSoTimeout(timeoutMs);
            s.startHandshake();

            StringBuilder head = new StringBuilder();
            head.append(method).append(' ').append(path).append(" HTTP/1.1\r\n");
            head.append("Host: ").append(HOST).append("\r\n");
            head.append("Accept: application/json\r\n");
            head.append("Connection: close\r\n");
            if (token != null && !token.isEmpty()) {
                head.append("Authorization: Bearer ").append(token).append("\r\n");
            }
            byte[] payload = body == null ? new byte[0] : body.getBytes(StandardCharsets.UTF_8);
            if (payload.length > 0) {
                head.append("Content-Type: application/json; charset=utf-8\r\n");
                head.append("Content-Length: ").append(payload.length).append("\r\n");
            }
            head.append("\r\n");

            OutputStream os = s.getOutputStream();
            os.write(head.toString().getBytes(StandardCharsets.UTF_8));
            if (payload.length > 0) os.write(payload);
            os.flush();

            byte[] all = readAll(s.getInputStream());
            int split = indexOf(all, new byte[]{'\r', '\n', '\r', '\n'});
            if (split < 0) throw new IOException("响应头不完整");
            String headerText = new String(all, 0, split, StandardCharsets.ISO_8859_1);
            int bodyStart = split + 4;
            String[] lines = headerText.split("\r\n");
            int code = 0;
            String[] first = lines[0].split(" ");
            if (first.length > 1) {
                try {
                    code = Integer.parseInt(first[1].trim());
                } catch (Exception ignored) {
                }
            }
            boolean chunked = headerText.toLowerCase(Locale.ROOT).contains("transfer-encoding: chunked");
            byte[] bodyBytes = new byte[all.length - bodyStart];
            System.arraycopy(all, bodyStart, bodyBytes, 0, bodyBytes.length);
            if (chunked) bodyBytes = dechunk(bodyBytes);
            String text = new String(bodyBytes, StandardCharsets.UTF_8);
            JSONObject json;
            try {
                json = new JSONObject(text);
            } catch (Exception e) {
                json = new JSONObject();
            }
            return new Resp(code, json);
        } finally {
            try {
                if (s != null) s.close();
            } catch (Exception ignored) {
            }
            try {
                plain.close();
            } catch (Exception ignored) {
            }
        }
    }

    private static byte[] readAll(InputStream is) throws IOException {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int n;
        while ((n = is.read(buf)) > 0) bos.write(buf, 0, n);
        return bos.toByteArray();
    }

    private static int indexOf(byte[] data, byte[] pat) {
        outer:
        for (int i = 0; i + pat.length <= data.length; i++) {
            for (int j = 0; j < pat.length; j++) {
                if (data[i + j] != pat[j]) continue outer;
            }
            return i;
        }
        return -1;
    }

    /** 解 chunked：长度行 + 数据 + CRLF，直到 0 长度 */
    private static byte[] dechunk(byte[] in) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        int pos = 0;
        while (pos < in.length) {
            int lineEnd = -1;
            for (int i = pos; i + 1 < in.length; i++) {
                if (in[i] == '\r' && in[i + 1] == '\n') {
                    lineEnd = i;
                    break;
                }
            }
            if (lineEnd < 0) break;
            String sizeLine = new String(in, pos, lineEnd - pos, StandardCharsets.ISO_8859_1).trim();
            int semi = sizeLine.indexOf(';');
            if (semi >= 0) sizeLine = sizeLine.substring(0, semi);
            int size;
            try {
                size = Integer.parseInt(sizeLine.trim(), 16);
            } catch (Exception e) {
                break;
            }
            pos = lineEnd + 2;
            if (size == 0) break;
            if (pos + size > in.length) size = in.length - pos;
            out.write(in, pos, size);
            pos += size + 2;
        }
        return out.toByteArray();
    }

    public static Resp login(String id, String password) throws Exception {
        JSONObject b = new JSONObject();
        b.put("id", id);
        b.put("password", password);
        return raw("POST", "/dianming/api/auth/login", null, b.toString(), 15000);
    }

    public static Resp roster(String token) throws Exception {
        return raw("GET", "/dianming/api/api/roster", token, null, 15000);
    }

    public static Resp upload(String token, String dateStr, JSONArray n) throws Exception {
        JSONObject b = new JSONObject();
        b.put("type", "marked");
        b.put("v", 4);
        b.put("ref", "");
        b.put("weekend", false);
        b.put("d", dateStr);
        b.put("n", n);
        return raw("POST", "/dianming/api/upload", token, b.toString(), 20000);
    }

    public static Resp appLatest() throws Exception {
        return raw("GET", "/dianming/api/api/app/latest", null, null, 15000);
    }

    public static Resp apkBase64(String file) throws Exception {
        return raw("GET", "/dianming/api/api/app/apk/" + file + "?b64=1", null, null, 90000);
    }

    public static Resp writeNow(String token) throws Exception {
        return raw("POST", "/dianming/api/write-now", token, "{}", 120000);
    }
}
