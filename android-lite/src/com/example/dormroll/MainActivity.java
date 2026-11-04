package com.example.dormroll;

import android.annotation.SuppressLint;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.net.Uri;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.security.MessageDigest;
import java.nio.charset.StandardCharsets;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.TimeUnit;

/**
 * 安卓客户端：与 iPad 版同一套交互与接口。
 * 选层 → 房卡（每房 8 床）→ 点床标缺勤并选四维 → 上传 → 写入共享表。
 */
public class MainActivity extends Activity {

    private static final String[] FLOORS = {"b1", "b2", "b3", "a2", "a3", "a4", "a5"};
    private static final String[] FLOOR_CN = {"B座一层", "B座二层", "B座三层",
            "A座二层", "A座三层", "A座四层", "A座五层"};
    private static final String[] DIMS = {"宿管办·事假", "宿管办·病假", "部系·事假", "部系·病假"};
    private static final String[] DIM_KEYS = {"DS", "DB", "XS", "XB"};

    private final ExecutorService pool = Executors.newSingleThreadExecutor();
    private final Handler ui = new Handler(Looper.getMainLooper());

    private SharedPreferences prefs;
    private String token = "", label = "", building = "B";
    private int floor = 1;

    private JSONArray rows = new JSONArray();          // 名册房间
    private final LinkedHashMap<String, String> marks = new LinkedHashMap<>();  // "房号#床" -> DS/DB/XS/XB
    private String lastNote = "";
    private String defaultQ = "DS";
    private long lastUpdCheck = 0;      // 当前假别：点人即按它记，和 iPad 一致

    private LinearLayout root;
    private LinearLayout content;
    private TextView subTitle, noteView;
    private Button uploadBtn, writeBtn;
    private WebView bridge;
    private volatile boolean bridgeReady = false;
    private final ConcurrentHashMap<String, Object[]> pending = new ConcurrentHashMap<>();

    @Override
    protected void onResume() {
        super.onResume();
        if (System.currentTimeMillis() - lastUpdCheck > 60_000) checkUpdate(false);   // 回前台自动查（60 秒节流）
    }

    @Override
    protected void onCreate(Bundle b) {
        super.onCreate(b);
        prefs = getSharedPreferences("dianming", MODE_PRIVATE);
        token = prefs.getString("token", "");
        label = prefs.getString("label", "");
        building = prefs.getString("building", "B");
        floor = prefs.getInt("floor", 1);
        initBridge();
        ui.postDelayed(() -> checkUpdate(false), 4000);        // 启动几秒后查一次新版本
        if (token.isEmpty()) showPicker();
        else showRollCall();
    }

    /** 假别条：当前选中高亮 */
    private void refreshQBar(LinearLayout bar) {
        if (bar == null) return;
        for (int i = 0; i < bar.getChildCount(); i++) {
            View c = bar.getChildAt(i);
            if (!(c instanceof Button) || c.getTag() == null) continue;
            boolean on = defaultQ.equals(String.valueOf(c.getTag()));
            c.setBackgroundColor(on ? Color.parseColor("#C0392B") : Color.parseColor("#EFEFF3"));
            ((Button) c).setTextColor(on ? Color.WHITE : Color.parseColor("#1B1B1F"));
        }
    }

    // ---------------- OTA：查版本 → 下载 → 校验 → 安装 ----------------
    private void checkUpdate(boolean manual) {
        pool.execute(() -> {
            if (manual) ui.post(() -> toast("检查更新中…"));
            try {
                Api.Resp r = Api.appLatest();
                if (!r.ok()) return;
                int remote = r.json.optInt("versionCode", 0);
                int local = getPackageManager().getPackageInfo(getPackageName(), 0).versionCode;
                lastUpdCheck = System.currentTimeMillis();
                if (remote <= local) {
                    if (manual) ui.post(() -> toast("已是最新 " + r.json.optString("versionName", "")));
                    return;
                }
                if (getPreferences(MODE_PRIVATE).getInt("skip_version", 0) == remote) {
                    if (manual) ui.post(() -> toast("已跳过这个版本"));
                    return;
                }
                String ver = r.json.optString("versionName", "?");
                String file = r.json.optString("file", "");
                String sha = r.json.optString("sha256", "");
                ui.post(() -> askUpdate(ver, file, sha, remote));
            } catch (Exception e) {
                if (manual) ui.post(() -> toast("检查失败：网络或服务不可用"));
            }
        });
    }

    private void askUpdate(String ver, String file, String sha, int code) {
        if (isFinishing() || file.isEmpty()) return;
        new AlertDialog.Builder(this)
                .setTitle("发现新版本 " + ver)
                .setMessage("「立即更新」会马上下载并重启安装；「稍后」这次先不更，下次打开再问。更新不会丢数据。")
                .setPositiveButton("立即更新", (d, w) -> doUpdate(file, sha))
                .setNeutralButton("稍后", null)
                .setNegativeButton("跳过此版本", (d, w) -> {
                    getPreferences(MODE_PRIVATE).edit().putInt("skip_version", code).apply();
                    toast("已跳过 " + ver + "，有更新版本再提示");
                })
                .show();
    }

    private void doUpdate(String file, String sha) {
        toast("正在下载更新…");
        pool.execute(() -> {
            try {
                Api.Resp r = Api.apkBase64(file);
                if (!r.ok()) throw new java.io.IOException("下载失败");
                byte[] apk = Base64.decode(r.json.optString("b64"), Base64.DEFAULT);
                String got = sha256(apk);
                if (!sha.isEmpty() && !sha.equalsIgnoreCase(got)) {
                    throw new java.io.IOException("校验不符，已拒绝安装");
                }
                File dir = new File(getCacheDir(), "ota");
                if (!dir.exists() && !dir.mkdirs()) throw new java.io.IOException("缓存目录不可写");
                File out = new File(dir, "update.apk");
                try (FileOutputStream fo = new FileOutputStream(out)) {
                    fo.write(apk);
                }
                ui.post(() -> {
                    toast("下载完成，正在打开安装器…");
                    installApk(out);
                });
            } catch (Exception e) {
                ui.post(() -> toast("更新失败：" + e.getMessage()));
            }
        });
    }

    private static String sha256(byte[] b) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] d = md.digest(b);
        StringBuilder sb = new StringBuilder();
        for (byte x : d) sb.append(String.format("%02x", x));
        return sb.toString();
    }

    private void installApk(File apk) {
        if (Build.VERSION.SDK_INT >= 26 && !getPackageManager().canRequestPackageInstalls()) {
            toast("请允许「安装未知应用」后重试");
            try {
                startActivity(new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                        Uri.parse("package:" + getPackageName())));
            } catch (Exception ignored) {
            }
            return;
        }
        Uri uri = Uri.parse("content://" + ApkProvider.AUTHORITY + "/" + apk.getName());
        Intent i = new Intent(Intent.ACTION_VIEW);
        i.setDataAndType(uri, "application/vnd.android.package-archive");
        i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK);
        startActivity(i);
    }

    // ---------------- 网络桥：走 Chromium 栈（云盾只放浏览器 TLS） ----------------
    private void setScreen(View v) {
        FrameLayout f = new FrameLayout(this);
        f.addView(v, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        if (bridge != null) {
            // WebView 只能有一个父：切屏时先摘下来再挂
            if (bridge.getParent() instanceof ViewGroup) {
                ((ViewGroup) bridge.getParent()).removeView(bridge);
            }
            f.addView(bridge, new FrameLayout.LayoutParams(1, 1));
        }
        setContentView(f);
    }

    @SuppressLint("SetJavaScriptEnabled")
    private void initBridge() {
        bridge = new WebView(this);
        WebSettings st = bridge.getSettings();
        st.setJavaScriptEnabled(true);
        st.setDomStorageEnabled(true);
        st.setCacheMode(WebSettings.LOAD_DEFAULT);
        bridge.setVisibility(View.INVISIBLE);
        bridge.addJavascriptInterface(new JsBridge(), "RCBridge");
        bridge.setWebChromeClient(new android.webkit.WebChromeClient() {
            @Override
            public boolean onConsoleMessage(android.webkit.ConsoleMessage m) {
                android.util.Log.w("dianming", "JS控制台: " + m.message() + " @" + m.lineNumber());
                return true;
            }
        });
        bridge.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                bridgeReady = true;
                android.util.Log.i("dianming", "桥就绪 " + url);
            }

            @Override
            public void onReceivedError(WebView view, android.webkit.WebResourceRequest req,
                                        android.webkit.WebResourceError err) {
                android.util.Log.w("dianming", "桥页面错误: " + err.getDescription());
            }
        });
        // 关键：用 loadDataWithBaseURL 把 origin 钉在 https://example.com:8443，
        // 这样页面里的 fetch 是"同源"，不触发 CORS，也不用等远端页面加载完
        bridge.loadUrl("https://example.com/dianming/api/manifest");

        Api.transport = (method, path, token, body, timeoutMs) -> {
            long waitStart = System.currentTimeMillis();
            while (!bridgeReady && System.currentTimeMillis() - waitStart < 12000) {
                Thread.sleep(200);           // 等 WebView 把同源页面加载出来，否则 fetch 无 origin
            }
            String id = java.util.UUID.randomUUID().toString().replace("-", "");
            final Object[] box = new Object[1];
            final CountDownLatch latch = new CountDownLatch(1);
            pending.put(id, new Object[]{box, latch});
            StringBuilder js = new StringBuilder();
            js.append("(async()=>{let last;for(let i=0;i<3;i++){try{")
              .append("const ac=new AbortController();const tm=setTimeout(()=>ac.abort(),15000);")
              .append("const t0=Date.now();const r=await fetch(")
              .append(JSONObject.quote(path))
              .append(",{method:").append(JSONObject.quote(method)).append(",signal:ac.signal,headers:{");
            js.append("'Content-Type':'application/json'");
            if (token != null && !token.isEmpty()) {
                js.append(",'Authorization':").append(JSONObject.quote("Bearer " + token));
            }
            js.append("}");
            if (body != null && !body.isEmpty()) {
                js.append(",body:").append(JSONObject.quote(body));
            }
            js.append("});clearTimeout(tm);")
              .append("if(r.status>=500){throw new Error('HTTP '+r.status);}")
              .append("const t=await r.text();console.log('bridge '+r.status+' '+(Date.now()-t0)+'ms try'+i);")
              .append("RCBridge.res(").append(JSONObject.quote(id)).append(",r.status,t);return;")
              .append("}catch(e){last=e;console.log('retry '+i+' '+e);await new Promise(x=>setTimeout(x,400*(i+1)));}}")
              .append("RCBridge.err(").append(JSONObject.quote(id)).append(",String(last));})()");
            runOnUiThread(() -> bridge.evaluateJavascript(js.toString(), null));
            try {
                if (!latch.await(Math.max(8000, timeoutMs), TimeUnit.MILLISECONDS)) {
                    pending.remove(id);
                    throw new java.io.IOException("请求超时");
                }
            } catch (InterruptedException e) {
                throw new java.io.IOException("被中断");
            }
            Object out = box[0];
            if (out == null) throw new java.io.IOException("无响应");
            if (out instanceof String) throw new java.io.IOException((String) out);
            Object[] pair = (Object[]) out;
            int code = (Integer) pair[0];
            String text = (String) pair[1];
            JSONObject json;
            try {
                json = new JSONObject(text);
            } catch (Exception e) {
                json = new JSONObject();
            }
            return new Api.Resp(code, json);
        };
    }

    /** WebView 回调：必须是 public 具名类，匿名类的方法不会被 @JavascriptInterface 导出 */
    public class JsBridge {
        @JavascriptInterface
        public void res(String id, int code, String text) {
            Object[] arr = pending.remove(id);
            if (arr != null) {
                ((Object[]) arr[0])[0] = new Object[]{code, text};   // 写进"盒子"，别覆盖盒子本身
                ((CountDownLatch) arr[1]).countDown();
            }
        }

        @JavascriptInterface
        public void err(String id, String msg) {
            android.util.Log.w("dianming", "JS 桥报错: " + msg);
            Object[] arr = pending.remove(id);
            if (arr != null) {
                ((Object[]) arr[0])[0] = msg;
                ((CountDownLatch) arr[1]).countDown();
            }
        }
    }

    // ---------------- 通用 UI ----------------
    private LinearLayout.LayoutParams lp(int w, int h) {
        return new LinearLayout.LayoutParams(w, h);
    }

    private TextView text(String s, int sp, int color, boolean bold) {
        TextView t = new TextView(this);
        t.setText(s);
        t.setTextSize(sp);
        t.setTextColor(color);
        if (bold) t.setTypeface(null, Typeface.BOLD);
        return t;
    }

    private Button button(String s, int color) {
        Button b = new Button(this);
        b.setText(s);
        b.setTextSize(15);
        b.setAllCaps(false);
        b.setTextColor(color);
        return b;
    }

    // ---------------- 一、选层 ----------------
    private void showPicker() {
        LinearLayout v = new LinearLayout(this);
        v.setOrientation(LinearLayout.VERTICAL);
        v.setBackgroundColor(Color.parseColor("#F5F6F8"));
        v.setPadding(dp(24), dp(40), dp(24), dp(24));

        v.addView(text("宿舍点名", 30, Color.parseColor("#1B1B1F"), true));
        TextView hint = text("选本层，进入后逐房点名", 15, Color.parseColor("#6B6B73"), false);
        hint.setPadding(0, dp(6), 0, dp(24));
        v.addView(hint);

        for (int r = 0; r < 4; r++) {
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            int start = r * 2;
            for (int c = 0; c < 2; c++) {
                int i = start + c;
                if (i >= FLOORS.length) break;
                Button btn = new Button(this);
                btn.setText(FLOOR_CN[i]);
                btn.setTextSize(17);
                btn.setAllCaps(false);
                btn.setBackgroundColor(Color.WHITE);
                btn.setTextColor(Color.parseColor("#1B1B1F"));
                LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, dp(72), 1f);
                p.setMargins(dp(6), dp(6), dp(6), dp(6));
                final String id = FLOORS[i];
                btn.setOnClickListener(x -> {
                    android.util.Log.i("dianming", "点击楼层 " + id);
                    doLogin(id);
                });
                row.addView(btn, p);
            }
            v.addView(row);
        }

        // 底部：检查更新 + 版本号
        String ver;
        try {
            ver = getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            ver = "?";
        }
        Button upd = button("检查更新 · 当前 v" + ver, Color.parseColor("#EFEFF3"));
        upd.setTextSize(13);
        upd.setTextColor(Color.parseColor("#1B1B1F"));
        LinearLayout.LayoutParams up = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, dp(44));
        up.setMargins(dp(6), dp(18), dp(6), 0);
        upd.setOnClickListener(x -> checkUpdate(true));
        v.addView(upd, up);

        setScreen(v);
    }

    private void doLogin(final String id) {
        android.util.Log.i("dianming", "开始登录 " + id);
        toast("登录中…");
        pool.execute(() -> {
            try {
                Api.Resp r = Api.login(id, id + "-CHANGE_ME");
                if (!r.ok()) {
                    ui.post(() -> toast("登录失败：" + r.json.optString("error", "口令不对")));
                    return;
                }
                token = r.json.optString("access_token");
                label = r.json.optString("label");
                JSONObject sc = r.json.optJSONObject("scope");
                building = sc != null ? sc.optString("building", "B") : "B";
                floor = sc != null ? sc.optInt("floor", 1) : 1;
                prefs.edit().putString("token", token).putString("label", label)
                        .putString("building", building).putInt("floor", floor).apply();
                marks.clear();
                ui.post(this::showRollCall);
            } catch (Exception e) {
                ui.post(() -> toast("网络不通：" + e.getMessage()));
            }
        });
    }

    // ---------------- 二、点名主界面 ----------------
    private void showRollCall() {
        LinearLayout v = new LinearLayout(this);
        v.setOrientation(LinearLayout.VERTICAL);
        v.setBackgroundColor(Color.parseColor("#F5F6F8"));

        LinearLayout head = new LinearLayout(this);
        head.setOrientation(LinearLayout.VERTICAL);
        head.setBackgroundColor(Color.WHITE);
        head.setPadding(dp(18), dp(14), dp(18), dp(12));
        LinearLayout titleRow = new LinearLayout(this);
        titleRow.setOrientation(LinearLayout.HORIZONTAL);
        titleRow.setGravity(Gravity.CENTER_VERTICAL);
        TextView title = text(label.isEmpty() ? "点名" : label, 22, Color.parseColor("#1B1B1F"), true);
        titleRow.addView(title, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f));
        Button refresh = button("刷新", Color.parseColor("#0B6B4F"));
        refresh.setTextSize(13);
        refresh.setOnClickListener(x -> { toast("正在刷新名册…"); loadRoster(); });
        titleRow.addView(refresh, new LinearLayout.LayoutParams(dp(90), dp(40)));
        head.addView(titleRow);
        subTitle = text("", 14, Color.parseColor("#6B6B73"), false);
        subTitle.setPadding(0, dp(4), 0, 0);
        head.addView(subTitle);

        LinearLayout qbar = new LinearLayout(this);
        qbar.setOrientation(LinearLayout.HORIZONTAL);
        qbar.setPadding(0, dp(8), 0, 0);
        for (int i = 0; i < DIMS.length; i++) {
            final String key = DIM_KEYS[i];
            Button b = button(dimShort(key), Color.parseColor("#1B1B1F"));
            b.setTextSize(13);
            b.setTag(key);
            b.setOnClickListener(x -> { defaultQ = key; refreshQBar((LinearLayout) ((View) x).getParent()); });
            LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, dp(40), 1f);
            p.setMargins(dp(2), 0, dp(2), 0);
            qbar.addView(b, p);
        }
        Button all = button("全部设为", Color.parseColor("#0B6B4F"));
        all.setTextSize(13);
        all.setOnClickListener(x -> {
            for (String k : marks.keySet()) marks.put(k, defaultQ);
            render();
            toast("已全部设为" + dimShort(defaultQ));
        });
        qbar.addView(all, new LinearLayout.LayoutParams(0, dp(40), 1.1f));
        head.addView(qbar);
        v.addView(head);
        refreshQBar(qbar);

        ScrollView sc = new ScrollView(this);
        content = new LinearLayout(this);
        content.setOrientation(LinearLayout.VERTICAL);
        content.setPadding(dp(12), dp(10), dp(12), dp(90));
        sc.addView(content);
        v.addView(sc, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f));

        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setBackgroundColor(Color.WHITE);
        bar.setPadding(dp(10), dp(8), dp(10), dp(8));
        bar.setGravity(Gravity.CENTER_VERTICAL);

        Button list = button("缺勤", Color.parseColor("#1B1B1F"));
        list.setOnClickListener(x -> showAbsentList());
        bar.addView(list, new LinearLayout.LayoutParams(0, dp(52), 1f));

        uploadBtn = button("上传", Color.parseColor("#0B6B4F"));
        uploadBtn.setOnClickListener(x -> confirmUpload());
        bar.addView(uploadBtn, new LinearLayout.LayoutParams(0, dp(52), 1f));

        writeBtn = button("写入表", Color.parseColor("#8A4B00"));
        writeBtn.setOnClickListener(x -> confirmWrite());
        bar.addView(writeBtn, new LinearLayout.LayoutParams(0, dp(52), 1f));

        Button out = button("退出", Color.parseColor("#7A7A82"));
        out.setOnClickListener(x -> confirmLogout());
        bar.addView(out, new LinearLayout.LayoutParams(0, dp(52), 1f));
        v.addView(bar);

        root = v;
        setScreen(v);
        loadRoster();
    }

    private void loadRoster() {
        String raw = readCache();
        if (raw != null) {
            try {
                JSONObject o = new JSONObject(raw);
                rows = o.optJSONArray("dormData");
                if (rows == null) rows = new JSONArray();
                render();
            } catch (Exception ignored) {
            }
        }
        pool.execute(() -> {
            try {
                Api.Resp r = Api.roster(token);
                if (r.code == 401) {
                    ui.post(this::logout);
                    return;
                }
                JSONArray data = r.json.optJSONArray("dormData");
                if (data == null && !r.ok()) return;      // 名册接口不带 ok 字段，认 dormData
                if (data == null || data.length() == 0) return;
                long ver = r.json.optLong("roster_version", 0);
                long old = prefs.getLong("rosterVersion", 0);
                boolean changed = ver > old || rows.length() == 0;
                rows = data;
                writeCache(r.json.toString());
                prefs.edit().putLong("rosterVersion", ver).apply();
                ui.post(() -> {
                    if (changed && !marks.isEmpty()) {
                        marks.clear();
                        lastNote = "名册已更新，旧标记已清除";
                    }
                    render();
                });
            } catch (Exception e) {
                ui.post(() -> {
                    lastNote = "网络不通，正在用本地名册";
                    render();
                });
            }
        });
    }

    private void render() {
        if (content == null) return;
        content.removeAllViews();
        int total = 0;
        for (int i = 0; i < rows.length(); i++) {
            JSONObject room = rows.optJSONObject(i);
            if (room == null) continue;
            total += occupied(room);
            content.addView(roomCard(room));
        }
        int absent = marks.size();
        subTitle.setText(String.format(Locale.CHINA, "应到 %d · 缺勤 %d · %s",
                total, absent, today()));
        if (uploadBtn != null) uploadBtn.setText(absent > 0 ? "上传 " + absent : "上传");
        if (!lastNote.isEmpty()) {
            noteView = text(lastNote, 13, Color.parseColor("#8A4B00"), false);
            noteView.setPadding(dp(18), dp(6), dp(18), 0);
            content.addView(noteView, 0);
        }
    }

    private int occupied(JSONObject room) {
        JSONArray st = room.optJSONArray("students");
        int n = 0;
        if (st != null) for (int i = 0; i < st.length(); i++) {
            JSONObject s = st.optJSONObject(i);
            if (s != null && !s.optString("name").isEmpty()) n++;
        }
        return n;
    }

    private View roomCard(JSONObject room) {
        final String roomNo = room.optString("room");
        String cls = room.optString("className", "");

        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setBackgroundColor(Color.WHITE);
        card.setPadding(dp(12), dp(10), dp(12), dp(10));
        LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        cp.setMargins(0, dp(6), 0, dp(6));
        card.setLayoutParams(cp);

        LinearLayout t = new LinearLayout(this);
        t.setOrientation(LinearLayout.HORIZONTAL);
        t.setGravity(Gravity.CENTER_VERTICAL);
        t.addView(text(roomNo + " 房", 17, Color.parseColor("#1B1B1F"), true));
        TextView c = text("  " + cls, 13, Color.parseColor("#6B6B73"), false);
        t.addView(c);
        card.addView(t);

        LinearLayout grid = new LinearLayout(this);
        grid.setOrientation(LinearLayout.VERTICAL);
        grid.setPadding(0, dp(8), 0, 0);
        JSONArray st = room.optJSONArray("students");
        for (int rowI = 0; rowI < 2; rowI++) {
            LinearLayout line = new LinearLayout(this);
            line.setOrientation(LinearLayout.HORIZONTAL);
            for (int col = 0; col < 4; col++) {
                int idx = rowI * 4 + col;
                JSONObject s = st != null && idx < st.length() ? st.optJSONObject(idx) : null;
                String nm = s == null ? "" : s.optString("name");
                String key = roomNo + "#" + idx;
                TextView chip = new TextView(this);
                chip.setTextSize(13);
                chip.setGravity(Gravity.CENTER);
                chip.setPadding(dp(2), dp(8), dp(2), dp(8));
                LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, dp(46), 1f);
                p.setMargins(dp(3), dp(3), dp(3), dp(3));
                if (nm.isEmpty()) {
                    chip.setText("空");
                    chip.setTextColor(Color.parseColor("#B9B9C0"));
                    chip.setBackgroundColor(Color.parseColor("#F2F2F5"));
                    chip.setEnabled(false);
                } else {
                    String q = marks.get(key);
                    chip.setText(nm);
                    if (q == null) {
                        chip.setTextColor(Color.parseColor("#1B1B1F"));
                        chip.setBackgroundColor(Color.parseColor("#F2F6FF"));
                    } else {
                        chip.setText(nm + "\n" + dimShort(q));
                        chip.setTextColor(Color.WHITE);
                        chip.setBackgroundColor(Color.parseColor("#C0392B"));
                    }
                    final String name = nm;
                    final String roomKey = roomNo;
                    final int bed = idx;
                    chip.setOnClickListener(x -> {
                        if (marks.containsKey(key)) marks.remove(key);
                        else marks.put(key, defaultQ);
                        render();
                    });
                    chip.setOnLongClickListener(x -> { pickDim(roomKey, bed, name); return true; });
                }
                line.addView(chip, p);
            }
            grid.addView(line);
        }
        card.addView(grid);
        return card;
    }

    private void pickDim(final String room, final int bed, final String name) {
        final String key = room + "#" + bed;
        final String cur = marks.get(key);
        String[] items = new String[DIMS.length + 1];
        System.arraycopy(DIMS, 0, items, 0, DIMS.length);
        items[DIMS.length] = cur == null ? "取消（本就在寝）" : "取消缺勤";
        new AlertDialog.Builder(this)
                .setTitle(room + " 房 · " + name)
                .setItems(items, (d, which) -> {
                    if (which == DIMS.length) marks.remove(key);
                    else marks.put(key, DIM_KEYS[which]);
                    render();
                })
                .show();
    }

    private String dimShort(String q) {
        switch (q) {
            case "DS": return "宿事";
            case "DB": return "宿病";
            case "XS": return "部事";
            default: return "部病";
        }
    }

    private void showAbsentList() {
        if (marks.isEmpty()) {
            toast("还没有缺勤");
            return;
        }
        StringBuilder sb = new StringBuilder();
        for (Map.Entry<String, String> e : marks.entrySet()) {
            String[] k = e.getKey().split("#");
            sb.append(k[0]).append(" 房 · ").append(bedName(k[0], Integer.parseInt(k[1])))
                    .append("　").append(dimShort(e.getValue())).append('\n');
        }
        new AlertDialog.Builder(this).setTitle("缺勤 " + marks.size() + " 人")
                .setMessage(sb.toString()).setPositiveButton("知道了", null).show();
    }

    private String bedName(String roomNo, int bed) {
        for (int i = 0; i < rows.length(); i++) {
            JSONObject r = rows.optJSONObject(i);
            if (r != null && roomNo.equals(r.optString("room"))) {
                JSONArray st = r.optJSONArray("students");
                JSONObject s = st != null && bed < st.length() ? st.optJSONObject(bed) : null;
                return s == null ? "?" : s.optString("name");
            }
        }
        return "?";
    }

    // ---------------- 三、动作 ----------------
    private void confirmUpload() {
        if (marks.isEmpty()) {
            toast("还没有缺勤，先在房卡上点人");
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("上传本次点名（" + marks.size() + " 人）？")
                .setMessage(firstFew())
                .setNegativeButton("取消", null)
                .setPositiveButton("上传", (d, w) -> doUpload())
                .show();
    }

    private String firstFew() {
        List<String> names = new ArrayList<>();
        int i = 0;
        for (String k : marks.keySet()) {
            String[] p = k.split("#");
            names.add(bedName(p[0], Integer.parseInt(p[1])));
            if (++i >= 3) break;
        }
        return String.join("、", names) + (marks.size() > 3 ? " 等" : "");
    }

    private void doUpload() {
        final String when = new SimpleDateFormat("yyyy/M/d HH:mm:ss", Locale.CHINA).format(new Date());
        final JSONArray arr = new JSONArray();
        for (Map.Entry<String, String> e : marks.entrySet()) {
            String[] k = e.getKey().split("#");
            JSONArray one = new JSONArray();
            one.put(bedName(k[0], Integer.parseInt(k[1])));
            one.put(k[0]);
            one.put(e.getValue());
            arr.put(one);
        }
        uploadBtn.setEnabled(false);
        toast("上传中…");
        pool.execute(() -> {
            try {
                Api.Resp r = Api.upload(token, when, arr);
                ui.post(() -> {
                    uploadBtn.setEnabled(true);
                    if (r.ok()) {
                        lastNote = "已上传 · " + new SimpleDateFormat("HH:mm", Locale.CHINA).format(new Date());
                        toast("上传成功");
                    } else {
                        toast("上传失败：" + r.json.optString("error", "服务端拒绝了"));
                    }
                    render();
                });
            } catch (Exception e) {
                ui.post(() -> {
                    uploadBtn.setEnabled(true);
                    toast("上传失败：" + e.getMessage());
                });
            }
        });
    }

    private void confirmWrite() {
        new AlertDialog.Builder(this)
                .setTitle("写入共享表？")
                .setMessage("把本层今天的缺勤直接写进钉钉表（" + marks.size() + " 人），写后不易撤回。")
                .setNegativeButton("取消", null)
                .setPositiveButton("写入", (d, w) -> doWrite())
                .show();
    }

    private void doWrite() {
        writeBtn.setEnabled(false);
        toast("写入中…");
        pool.execute(() -> {
            try {
                Api.Resp r = Api.writeNow(token);
                ui.post(() -> {
                    writeBtn.setEnabled(true);
                    if (r.ok()) {
                        lastNote = "已写入共享表";
                        toast("已写入共享表");
                    } else if (r.json.optBoolean("skipped", false)) {
                        toast("没有可写的内容：请先上传，或稍后再试");
                    } else {
                        toast("写入未完成：" + r.json.optString("error", "未知原因"));
                    }
                    render();
                });
            } catch (Exception e) {
                ui.post(() -> {
                    writeBtn.setEnabled(true);
                    toast("写入失败：" + e.getMessage());
                });
            }
        });
    }

    private void confirmLogout() {
        new AlertDialog.Builder(this)
                .setTitle("退出本层？")
                .setMessage("未上传的标记会丢失。")
                .setNegativeButton("取消", null)
                .setPositiveButton("退出", (d, w) -> logout())
                .show();
    }

    private void logout() {
        token = "";
        label = "";
        marks.clear();
        prefs.edit().remove("token").apply();
        showPicker();
    }

    // ---------------- 四、本地缓存 ----------------
    private String cacheFile() {
        return "roster_" + building + floor + ".json";
    }

    private String readCache() {
        try {
            File f = new File(getFilesDir(), cacheFile());
            if (!f.exists()) return null;
            byte[] b = new byte[(int) f.length()];
            try (java.io.FileInputStream in = new java.io.FileInputStream(f)) {
                int off = 0;
                while (off < b.length) {
                    int n = in.read(b, off, b.length - off);
                    if (n < 0) break;
                    off += n;
                }
            }
            return new String(b, StandardCharsets.UTF_8);
        } catch (Exception e) {
            return null;
        }
    }

    private void writeCache(String s) {
        try (FileOutputStream out = openFileOutput(cacheFile(), Context.MODE_PRIVATE)) {
            out.write(s.getBytes(StandardCharsets.UTF_8));
        } catch (Exception ignored) {
        }
    }

    private String today() {
        return new SimpleDateFormat("M月d日", Locale.CHINA).format(new Date());
    }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }
}
