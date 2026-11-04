package com.example.dormroll;

import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.InputType;
import android.util.Base64;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.Menu;
import android.view.View;
import android.view.ViewGroup;
import android.widget.EditText;
import android.widget.GridLayout;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.annotation.NonNull;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.splashscreen.SplashScreen;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.recyclerview.widget.GridLayoutManager;
import androidx.recyclerview.widget.RecyclerView;
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

import com.google.android.material.appbar.MaterialToolbar;
import com.google.android.material.button.MaterialButton;
import com.google.android.material.card.MaterialCardView;
import com.google.android.material.dialog.MaterialAlertDialogBuilder;
import com.google.android.material.progressindicator.LinearProgressIndicator;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Date;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class MainActivity extends AppCompatActivity {

    private static final String[] FLOORS = {"b1", "b2", "b3", "a2", "a3", "a4", "a5"};
    private static final String[] FLOOR_CN = {"B座一层", "B座二层", "B座三层", "A座二层", "A座三层", "A座四层", "A座五层"};
    private static final String[] Q_CODES = {"DS", "DB", "XS", "XB"};
    private static final String[] Q_NAMES = {"宿事", "宿病", "部事", "部病"};

    private final Handler ui = new Handler(Looper.getMainLooper());
    private final ExecutorService pool = Executors.newFixedThreadPool(3);

    private WebTransport net;
    private SharedPreferences prefs;

    private String token = "", label = "", building = "B";
    private int floor = 1;
    private String defaultQ = "DS";
    private long lastUpdCheck = 0;

    private final List<Room> rooms = new ArrayList<>();
    private final Map<String, Set<Integer>> absent = new LinkedHashMap<>();
    private final Map<String, String> marks = new LinkedHashMap<>();

    private TextView barAbsent, checkNote, statTotal, statLeft, bottomHint, typeLine;
    private String lastWriteNote = "";
    private LinearProgressIndicator loadingBar;
    private MaterialButton uploadBtn;
    private RoomAdapter adapter;
    private RecyclerView list;
    private SwipeRefreshLayout refresh;

    // ---------------- 生命周期 ----------------

    @Override
    protected void onCreate(Bundle b) {
        SplashScreen.installSplashScreen(this);
        super.onCreate(b);
        prefs = getSharedPreferences("rc", MODE_PRIVATE);
        token = prefs.getString("token", "");
        label = prefs.getString("label", "");
        building = prefs.getString("building", "B");
        floor = prefs.getInt("floor", 1);

        net = new WebTransport(this, ok -> ui.post(this::route));
        ui.postDelayed(() -> checkUpdate(false), 4000);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (System.currentTimeMillis() - lastUpdCheck > 60_000) checkUpdate(false);
    }

    private void route() {
        if (token.isEmpty()) showPicker();
        else showRollCall();
    }

    // ---------------- 选层 ----------------

    private void showPicker() {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.setBackgroundColor(c(R.color.surface_bg));

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        scroll.addView(root, new ViewGroup.LayoutParams(-1, -2));
        root.addView(statusSpacer());

        // 顶部渐变头
        LinearLayout head = new LinearLayout(this);
        head.setOrientation(LinearLayout.VERTICAL);
        head.setBackgroundResource(R.drawable.bg_header);
        head.setPadding(dp(24), dp(46), dp(24), dp(22));
        root.addView(head, new ViewGroup.LayoutParams(-1, -2));

        TextView t1 = new TextView(this);
        t1.setText("宿舍点名");
        t1.setTextSize(30);
        t1.setTextColor(Color.WHITE);
        t1.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        head.addView(t1);

        TextView t2 = new TextView(this);
        t2.setText("选层进入 · 逐房点名 · 一键上传");
        t2.setTextSize(13);
        t2.setTextColor(0xCCFFFFFF);
        t2.setPadding(0, dp(8), 0, dp(18));
        head.addView(t2);

        TextView badge = new TextView(this);
        badge.setText("  教务处 · 住宿生点名  ");
        badge.setTextSize(12);
        badge.setTextColor(Color.WHITE);
        GradientDrawable pill = new GradientDrawable();
        pill.setCornerRadius(dp(20));
        pill.setColor(0x33FFFFFF);
        pill.setStroke(dp(1), 0x66FFFFFF);
        badge.setBackground(pill);
        badge.setPadding(dp(10), dp(6), dp(10), dp(6));
        head.addView(badge);

        // 楼层网格
        LinearLayout body = new LinearLayout(this);
        body.setOrientation(LinearLayout.VERTICAL);
        body.setPadding(dp(18), dp(14), dp(18), dp(16));
        root.addView(body, new ViewGroup.LayoutParams(-1, -2));

        TextView cap = new TextView(this);
        cap.setText("选择楼层");
        cap.setTextSize(13);
        cap.setTextColor(c(R.color.text_sub));
        cap.setPadding(dp(4), 0, 0, dp(10));
        body.addView(cap);

        RecyclerView grid = new RecyclerView(this);
        grid.setLayoutManager(new GridLayoutManager(this, 2));
        grid.setNestedScrollingEnabled(false);
        grid.setClipToPadding(false);
        FloorAdapter fa = new FloorAdapter();
        grid.setAdapter(fa);
        body.addView(grid, new ViewGroup.LayoutParams(-1, -2));

        // 底部：检查更新 + 版本
        MaterialButton upd = new MaterialButton(this, null,
                com.google.android.material.R.attr.materialButtonOutlinedStyle);
        upd.setText("检查更新");
        upd.setAllCaps(false);
        upd.setTextSize(13);
        LinearLayout.LayoutParams up = new LinearLayout.LayoutParams(-2, dp(44));
        up.topMargin = dp(18);
        up.gravity = Gravity.CENTER_HORIZONTAL;
        body.addView(upd, up);

        checkNote = new TextView(this);
        checkNote.setTextSize(11);
        checkNote.setTextColor(c(R.color.text_sub));
        checkNote.setGravity(Gravity.CENTER);
        checkNote.setPadding(0, dp(8), 0, 0);
        checkNote.setText("v" + versionName());
        body.addView(checkNote);

        upd.setOnClickListener(v -> checkUpdate(true));

        lockFont(scroll);
        setContentView(scroll);
    }

    private class FloorAdapter extends RecyclerView.Adapter<FloorVH> {
        @NonNull
        @Override
        public FloorVH onCreateViewHolder(@NonNull ViewGroup parent, int viewType) {
            MaterialCardView card = new MaterialCardView(parent.getContext());
            RecyclerView.LayoutParams lp = new RecyclerView.LayoutParams(-1, dp(104));
            lp.setMargins(dp(4), dp(4), dp(4), dp(4));
            card.setLayoutParams(lp);
            card.setRadius(dp(18));
            card.setCardElevation(dp(1));
            card.setStrokeWidth(dp(1));
            card.setStrokeColor(c(R.color.line));
            card.setCardBackgroundColor(c(R.color.surface_card));
            card.setClickable(true);
            card.setFocusable(true);

            LinearLayout box = new LinearLayout(parent.getContext());
            box.setOrientation(LinearLayout.VERTICAL);
            box.setGravity(Gravity.CENTER);
            box.setPadding(dp(8), dp(8), dp(8), dp(8));
            card.addView(box);

            TextView name = new TextView(parent.getContext());
            name.setTextSize(16);
            name.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
            name.setTextColor(c(R.color.text_main));
            name.setGravity(Gravity.CENTER);
            box.addView(name);

            TextView sub = new TextView(parent.getContext());
            sub.setTextSize(11);
            sub.setTextColor(c(R.color.text_sub));
            sub.setGravity(Gravity.CENTER);
            sub.setPadding(0, dp(4), 0, 0);
            box.addView(sub);

            return new FloorVH(card, name, sub);
        }

        @Override
        public void onBindViewHolder(@NonNull FloorVH h, int pos) {
            h.name.setText(FLOOR_CN[pos]);
            boolean isA = FLOORS[pos].startsWith("a");
            h.sub.setText(isA ? "A 座 · 女生" : "B 座 · 男生");
            h.sub.setTextColor(isA ? c(R.color.gold) : c(R.color.brand));
            h.itemView.setOnClickListener(v -> doLogin(FLOORS[pos]));
        }

        @Override
        public int getItemCount() { return FLOORS.length; }
    }

    private static class FloorVH extends RecyclerView.ViewHolder {
        final TextView name, sub;
        FloorVH(View v, TextView n, TextView s) { super(v); name = n; sub = s; }
    }

    private void doLogin(String id) {
        toast("正在登录…");
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
                absent.clear();
                marks.clear();
                ui.post(this::showRollCall);
            } catch (Exception e) {
                ui.post(() -> toast("网络不通：" + e.getMessage()));
            }
        });
    }

    // ---------------- 点名页 ----------------

    private void showRollCall() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(c(R.color.surface_bg));

        root.addView(statusSpacer());

        MaterialToolbar tb = new MaterialToolbar(this);
        tb.setTitle(label.isEmpty() ? "点名" : label);
        tb.setSubtitle("住宿生点名");
        tb.setTitleTextColor(Color.WHITE);
        tb.setSubtitleTextColor(0xCCFFFFFF);
        tb.setBackgroundColor(c(R.color.brand));
        tb.setNavigationIcon(android.R.drawable.ic_menu_revert);
        tb.setNavigationOnClickListener(v -> requestBack());
        tb.setNavigationIconTint(Color.WHITE);
        tb.inflateMenu(R.menu.roll_menu);
        android.graphics.drawable.Drawable ov = tb.getOverflowIcon();
        if (ov != null) { ov.setTint(Color.WHITE); tb.setOverflowIcon(ov); }
        tb.setOnMenuItemClickListener(item -> {
            int id = item.getItemId();
            if (id == R.id.m_refresh) { loadRoster(); return true; }
            if (id == R.id.m_all_present) { absent.clear(); refreshList(); return true; }
            if (id == R.id.m_absent_list) { showAbsentList(); return true; }
            if (id == R.id.m_reset) { confirmReset(); return true; }
            if (id == R.id.m_check) { checkUpdate(true); return true; }
            return false;
        });
        root.addView(tb, new ViewGroup.LayoutParams(-1, -2));

        loadingBar = new LinearProgressIndicator(this);
        loadingBar.setIndeterminate(true);
        loadingBar.setTrackThickness(dp(2));
        loadingBar.setIndicatorColor(c(R.color.gold));
        loadingBar.setTrackColor(c(R.color.brand));
        root.addView(loadingBar, new ViewGroup.LayoutParams(-1, dp(2)));

        // 统计条
        MaterialCardView statCard = new MaterialCardView(this);
        LinearLayout.LayoutParams sc = new LinearLayout.LayoutParams(-1, -2);
        sc.setMargins(dp(16), dp(12), dp(16), dp(6));
        statCard.setLayoutParams(sc);
        statCard.setRadius(dp(16));
        statCard.setCardElevation(dp(1));
        statCard.setStrokeWidth(dp(1));
        statCard.setStrokeColor(c(R.color.line));

        LinearLayout statWrap = new LinearLayout(this);
        statWrap.setOrientation(LinearLayout.VERTICAL);
        statCard.addView(statWrap);

        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(dp(8), dp(12), dp(8), dp(4));
        statWrap.addView(row);

        typeLine = new TextView(this);
        typeLine.setTextSize(11);
        typeLine.setTextColor(c(R.color.text_sub));
        typeLine.setGravity(Gravity.CENTER);
        typeLine.setPadding(dp(8), 0, dp(8), dp(10));
        typeLine.setVisibility(View.GONE);
        statWrap.addView(typeLine);

        TextView[] tvTotal = new TextView[1], tvAbs = new TextView[1], tvLeft = new TextView[1];
        row.addView(stat("应到", "0", c(R.color.text_main), tvTotal), weight());
        row.addView(stat("缺勤", "0", c(R.color.danger), tvAbs), weight());
        row.addView(stat("房间", "0", c(R.color.text_sub), tvLeft), weight());
        statTotal = tvTotal[0];
        barAbsent = tvAbs[0];
        statLeft = tvLeft[0];

        root.addView(statCard);

        // 列表
        refresh = new SwipeRefreshLayout(this);
        refresh.setOnRefreshListener(this::loadRoster);
        list = new RecyclerView(this);
        int cols = getResources().getConfiguration().screenWidthDp >= 600 ? 3 : 2;
        list.setLayoutManager(new GridLayoutManager(this, cols));
        list.setClipToPadding(false);
        list.setPadding(dp(12), dp(4), dp(12), dp(96));
        adapter = new RoomAdapter();
        list.setAdapter(adapter);
        refresh.addView(list);
        root.addView(refresh, new LinearLayout.LayoutParams(-1, 0, 1f));

        // 底部操作条
        MaterialCardView bottom = new MaterialCardView(this);
        LinearLayout.LayoutParams bc = new LinearLayout.LayoutParams(-1, -2);
        bottom.setLayoutParams(bc);
        bottom.setRadius(0);
        bottom.setCardElevation(dp(8));

        LinearLayout brow = new LinearLayout(this);
        brow.setOrientation(LinearLayout.HORIZONTAL);
        brow.setGravity(Gravity.CENTER_VERTICAL);
        brow.setPadding(dp(14), dp(10), dp(14), dp(10));
        bottom.addView(brow);

        LinearLayout.LayoutParams lpMark = new LinearLayout.LayoutParams(0, dp(44), 1f);
        lpMark.rightMargin = dp(6);
        MaterialButton markBtn = new MaterialButton(this, null,
                com.google.android.material.R.attr.materialButtonOutlinedStyle);
        markBtn.setText("标记");
        markBtn.setAllCaps(false);
        markBtn.setCornerRadius(dp(22));
        markBtn.setOnClickListener(v -> chooseQForAll());
        brow.addView(markBtn, lpMark);

        LinearLayout.LayoutParams lpWrite = new LinearLayout.LayoutParams(0, dp(44), 1f);
        lpWrite.rightMargin = dp(6);
        MaterialButton writeBtn = new MaterialButton(this, null,
                com.google.android.material.R.attr.materialButtonOutlinedStyle);
        writeBtn.setText("写表");
        writeBtn.setAllCaps(false);
        writeBtn.setCornerRadius(dp(22));
        writeBtn.setOnClickListener(v -> confirmWrite());
        brow.addView(writeBtn, lpWrite);

        uploadBtn = new MaterialButton(this);
        uploadBtn.setText("上传");
        uploadBtn.setAllCaps(false);
        uploadBtn.setCornerRadius(dp(22));
        uploadBtn.setOnClickListener(v -> doUpload());
        brow.addView(uploadBtn, new LinearLayout.LayoutParams(0, dp(44), 1.2f));

        root.addView(bottom, new ViewGroup.LayoutParams(-1, -2));
        lockFont(root);
        setContentView(root);

        loadRoster();
    }

    private LinearLayout stat(String cap, String val, int color, TextView[] out) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        TextView v = new TextView(this);
        v.setText(val);
        v.setTextSize(22);
        v.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
        v.setTextColor(color);
        v.setGravity(Gravity.CENTER);
        TextView c1 = new TextView(this);
        c1.setText(cap);
        c1.setTextSize(11);
        c1.setTextColor(c(R.color.text_sub));
        c1.setGravity(Gravity.CENTER);
        box.addView(v);
        box.addView(c1);
        out[0] = v;
        return box;
    }

    private LinearLayout.LayoutParams weight() {
        LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, -2, 1f);
        return p;
    }

    // ---------------- 名册 ----------------

    private void loadRoster() {
        if (refresh != null) refresh.setRefreshing(true);
        if (loadingBar != null) loadingBar.setVisibility(View.VISIBLE);
        pool.execute(() -> {
            try {
                Api.Resp r = Api.roster(token);
                if (r.code == 401) {
                    ui.post(this::logout);
                    return;
                }
                JSONArray data = r.json.optJSONArray("dormData");
                if (data == null) {
                    ui.post(() -> {
                        stopLoading();
                        toast("名册读取失败");
                    });
                    return;
                }
                List<Room> parsed = new ArrayList<>();
                for (int i = 0; i < data.length(); i++) {
                    JSONObject o = data.getJSONObject(i);
                    Room rm = new Room();
                    rm.room = o.optString("room");
                    rm.cls = o.optString("className");
                    JSONArray st = o.optJSONArray("students");
                    for (int k = 0; k < 8; k++) {
                        JSONObject s = st != null && k < st.length() ? st.optJSONObject(k) : null;
                        String nm = s == null ? "" : s.optString("name", "");
                        rm.beds.add(nm);
                    }
                    parsed.add(rm);
                }
                ui.post(() -> {
                    rooms.clear();
                    rooms.addAll(parsed);
                    adapter.notifyDataSetChanged();
                    stopLoading();
                    updateStats();
                });
            } catch (Exception e) {
                ui.post(() -> {
                    stopLoading();
                    toast("网络不通：" + e.getMessage());
                });
            }
        });
    }

    private void stopLoading() {
        if (refresh != null) refresh.setRefreshing(false);
        if (loadingBar != null) loadingBar.setVisibility(View.GONE);
    }

    private void refreshList() {
        if (adapter != null) adapter.notifyDataSetChanged();
        updateStats();
    }

    private void updateStats() {
        int total = 0;
        for (Room r : rooms) for (String n : r.beds) if (!n.isEmpty()) total++;
        int abs = 0;
        for (Set<Integer> s : absent.values()) abs += s.size();
        if (barAbsent != null) barAbsent.setText(String.valueOf(abs));
        if (statTotal != null) statTotal.setText(String.valueOf(total));
        if (statLeft != null) statLeft.setText(String.valueOf(rooms.size()));
        if (bottomHint != null) bottomHint.setText(lastWriteNote);

        if (typeLine != null) {
            int ds = 0, db = 0, xs = 0, xb = 0;
            for (Map.Entry<String, Set<Integer>> e : absent.entrySet()) {
                for (int bed : e.getValue()) {
                    String q = marks.getOrDefault(e.getKey() + "#" + bed, defaultQ);
                    switch (q) {
                        case "DS": ds++; break;
                        case "DB": db++; break;
                        case "XS": xs++; break;
                        default: xb++; break;
                    }
                }
            }
            if (ds + db + xs + xb == 0) {
                typeLine.setVisibility(View.GONE);
            } else {
                typeLine.setVisibility(View.VISIBLE);
                typeLine.setText("宿事 " + ds + " · 宿病 " + db + " · 部事 " + xs + " · 部病 " + xb);
            }
        }
        if (uploadBtn != null) uploadBtn.setText(abs == 0 ? "上传" : "上传 " + abs);
        if (getSupportActionBar() != null) getSupportActionBar().setSubtitle(label + " · 应到 " + total);
    }

    // ---------------- 房卡 ----------------

    private class RoomAdapter extends RecyclerView.Adapter<RoomVH> {
        @NonNull
        @Override
        public RoomVH onCreateViewHolder(@NonNull ViewGroup parent, int viewType) {
            MaterialCardView card = new MaterialCardView(parent.getContext());
            RecyclerView.LayoutParams lp = new RecyclerView.LayoutParams(-1, -2);
            lp.setMargins(dp(4), dp(4), dp(4), dp(4));
            card.setLayoutParams(lp);
            card.setRadius(dp(18));
            card.setCardElevation(dp(1));
            card.setStrokeWidth(dp(1));
            card.setStrokeColor(c(R.color.line));

            LinearLayout box = new LinearLayout(parent.getContext());
            box.setOrientation(LinearLayout.VERTICAL);
            box.setPadding(dp(11), dp(9), dp(11), dp(11));
            card.addView(box);

            LinearLayout head = new LinearLayout(parent.getContext());
            head.setOrientation(LinearLayout.HORIZONTAL);
            head.setGravity(Gravity.CENTER_VERTICAL);
            box.addView(head);

            TextView room = new TextView(parent.getContext());
            room.setTextSize(18);
            room.setTypeface(Typeface.create("sans-serif-medium", Typeface.BOLD));
            room.setTextColor(c(R.color.text_main));
            head.addView(room);

            TextView cls = new TextView(parent.getContext());
            cls.setTextSize(10);
            cls.setTextColor(c(R.color.brand));
            cls.setPadding(dp(8), dp(2), dp(8), dp(2));
            GradientDrawable pill = new GradientDrawable();
            pill.setCornerRadius(dp(10));
            pill.setColor(c(R.color.primary_pill));
            cls.setBackground(pill);
            LinearLayout.LayoutParams cp = new LinearLayout.LayoutParams(-2, -2);
            cp.leftMargin = dp(8);
            head.addView(cls, cp);

            TextView[] chips = new TextView[8];
            boolean wideGrid = getResources().getConfiguration().screenWidthDp >= 600;
            int cols = wideGrid ? 4 : 2;
            int rows = 8 / cols;
            for (int row = 0; row < rows; row++) {
                LinearLayout line = new LinearLayout(parent.getContext());
                line.setOrientation(LinearLayout.HORIZONTAL);
                LinearLayout.LayoutParams lp2 = new LinearLayout.LayoutParams(-1, -2);
                if (row > 0) lp2.topMargin = dp(4);
                box.addView(line, lp2);
                for (int c = 0; c < cols; c++) {
                    int i = row * cols + c;
                    TextView chip = new TextView(parent.getContext());
                    chip.setTextSize(13);
                    chip.setGravity(Gravity.CENTER);
                    chip.setSingleLine(true);
                    chip.setMaxLines(1);
                    chip.setEllipsize(android.text.TextUtils.TruncateAt.END);
                    chip.setIncludeFontPadding(false);
                    chip.setPadding(dp(2), dp(10), dp(2), dp(10));
                    LinearLayout.LayoutParams p = new LinearLayout.LayoutParams(0, dp(44), 1f);
                    p.setMargins(dp(3), 0, dp(3), 0);
                    line.addView(chip, p);
                    chips[i] = chip;
                }
            }

            return new RoomVH(card, room, cls, chips);
        }

        @Override
        public void onBindViewHolder(@NonNull RoomVH h, int pos) {
            Room r = rooms.get(pos);
            h.room.setText(r.room);
            h.cls.setText(r.cls);
            Set<Integer> set = absent.get(r.room);
            for (int i = 0; i < 8; i++) {
                TextView chip = h.chips[i];
                String nm = r.beds.get(i);
                boolean empty = nm.isEmpty();
                boolean off = set != null && set.contains(i);
                chip.setText(empty ? "" : nm);
                chip.setBackgroundResource(empty ? R.drawable.bg_bed_empty
                        : (off ? R.drawable.bg_bed_absent : R.drawable.bg_bed_normal));
                chip.setTextColor(empty ? 0x33000000 : (off ? Color.WHITE : c(R.color.text_main)));
                int len = nm.length();
                chip.setTextSize(len >= 4 ? 11f : 13f);
                chip.setTypeface(null, off ? Typeface.BOLD : Typeface.NORMAL);
                chip.setClickable(!empty);
                final int bed = i;
                chip.setOnClickListener(empty ? null : v -> toggle(r.room, bed));
                chip.setOnLongClickListener(empty ? null : v -> {
                    chooseQ(r.room, bed, r.beds.get(bed));
                    return true;
                });
            }
        }

        @Override
        public int getItemCount() { return rooms.size(); }
    }

    private static class RoomVH extends RecyclerView.ViewHolder {
        final TextView room, cls;
        final TextView[] chips;
        RoomVH(View v, TextView r, TextView c, TextView[] ch) {
            super(v); room = r; cls = c; chips = ch;
        }
    }

    private static class Room {
        String room = "", cls = "";
        final List<String> beds = new ArrayList<>();
    }

    private void toggle(String room, int bed) {
        Set<Integer> set = absent.computeIfAbsent(room, k -> new LinkedHashSet<>());
        if (set.contains(bed)) {
            set.remove(bed);
            marks.remove(room + "#" + bed);
        } else {
            set.add(bed);
            marks.put(room + "#" + bed, defaultQ);
        }
        if (set.isEmpty()) absent.remove(room);
        adapter.notifyDataSetChanged();
        updateStats();
    }

    /** 底部「标记」：列出每个人，各自选假别（同 iPad 的请假类型页） */
    private void chooseQForAll() {
        int n = 0;
        for (Set<Integer> s : absent.values()) n += s.size();
        if (n == 0) { toast("先点人选择，再设假别"); return; }

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(18), dp(6), dp(18), dp(6));

        for (Room r : rooms) {
            Set<Integer> set = absent.get(r.room);
            if (set == null) continue;
            for (int bed : set) {
                LinearLayout rowBox = new LinearLayout(this);
                rowBox.setOrientation(LinearLayout.VERTICAL);
                rowBox.setPadding(0, dp(8), 0, dp(8));

                TextView who = new TextView(this);
                who.setText(r.beds.get(bed) + "  ·  " + r.room + " · " + (bed + 1) + " 号铺");
                who.setTextSize(14);
                who.setTextColor(c(R.color.text_main));
                rowBox.addView(who);

                com.google.android.material.button.MaterialButtonToggleGroup tg =
                        new com.google.android.material.button.MaterialButtonToggleGroup(this);
                tg.setSingleSelection(true);
                tg.setSelectionRequired(true);
                LinearLayout.LayoutParams tp = new LinearLayout.LayoutParams(-1, dp(38));
                tp.topMargin = dp(6);
                rowBox.addView(tg, tp);

                String cur = marks.getOrDefault(r.room + "#" + bed, defaultQ);
                for (int k = 0; k < 4; k++) {
                    MaterialButton b = new MaterialButton(this, null,
                            com.google.android.material.R.attr.materialButtonOutlinedStyle);
                    b.setText(Q_NAMES[k]);
                    b.setTextSize(11);
                    b.setAllCaps(false);
                    b.setPadding(0, 0, 0, 0);
                    b.setId(1000 + k);
                    LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(0, dp(36), 1f);
                    bp.setMargins(dp(2), 0, dp(2), 0);
                    tg.addView(b, bp);
                    if (Q_CODES[k].equals(cur)) tg.check(b.getId());
                    final String room = r.room;
                    final int bedIdx = bed;
                    final String qCode = Q_CODES[k];
                    b.setOnClickListener(v -> marks.put(room + "#" + bedIdx, qCode));
                }
                box.addView(rowBox);

                View line = new View(this);
                line.setBackgroundColor(c(R.color.line));
                box.addView(line, new LinearLayout.LayoutParams(-1, dp(1)));
            }
        }

        ScrollView sc = new ScrollView(this);
        sc.addView(box);

        final int total = n;
        new MaterialAlertDialogBuilder(this)
                .setTitle("请假类型（" + total + " 人）")
                .setView(sc)
                .setPositiveButton("完成", (d, w) -> {
                    adapter.notifyDataSetChanged();
                    updateStats();
                    toast("已更新 " + total + " 人的假别");
                })
                .setNeutralButton("全部设为", (d, w) -> setAllTypes())
                .show();
    }

    private void setAllTypes() {
        String[] items = {"宿管 · 事假", "宿管 · 病假", "部系 · 事假", "部系 · 病假"};
        new MaterialAlertDialogBuilder(this)
                .setTitle("全部设为")
                .setItems(items, (d, which) -> {
                    String q = Q_CODES[which];
                    for (Map.Entry<String, Set<Integer>> e : absent.entrySet()) {
                        for (int bed : e.getValue()) marks.put(e.getKey() + "#" + bed, q);
                    }
                    defaultQ = q;
                    adapter.notifyDataSetChanged();
                    updateStats();
                    toast("全部设为 " + items[which]);
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void chooseQ(String room, int bed, String name) {
        new MaterialAlertDialogBuilder(this)
                .setTitle(name + " · " + room + " · " + (bed + 1) + " 号铺")
                .setItems(Q_NAMES, (d, which) -> {
                    absent.computeIfAbsent(room, k -> new LinkedHashSet<>()).add(bed);
                    marks.put(room + "#" + bed, Q_CODES[which]);
                    defaultQ = Q_CODES[which];
                    adapter.notifyDataSetChanged();
                    updateStats();
                })
                .setNegativeButton("取消", null)
                .show();
    }

    // ---------------- 操作 ----------------

    private void doUpload() {
        int n = 0;
        for (Set<Integer> s : absent.values()) n += s.size();
        if (n == 0) { toast("还没标缺勤"); return; }

        StringBuilder who = new StringBuilder();
        int k = 0;
        for (Room r : rooms) {
            Set<Integer> s = absent.get(r.room);
            if (s == null) continue;
            for (int i : s) {
                if (k++ >= 3) break;
                who.append(r.beds.get(i)).append("、");
            }
        }
        String preview = who.toString().replaceAll("、$", "") + (n > 3 ? " 等" : "");

        new MaterialAlertDialogBuilder(this)
                .setTitle("上传本次点名")
                .setMessage("共 " + n + " 人缺勤：" + preview)
                .setPositiveButton("上传", (d, w) -> uploadNow())
                .setNegativeButton("取消", null)
                .show();
    }

    private void uploadNow() {
        JSONArray arr = new JSONArray();
        try {
            for (Room r : rooms) {
                Set<Integer> s = absent.get(r.room);
                if (s == null) continue;
                for (int i : s) {
                    JSONArray one = new JSONArray();
                    one.put(r.beds.get(i));
                    one.put(r.room);
                    one.put(marks.getOrDefault(r.room + "#" + i, defaultQ));
                    arr.put(one);
                }
            }
        } catch (Exception ignored) {
        }
        String when = new SimpleDateFormat("yyyy/M/d HH:mm:ss", Locale.CHINA).format(new Date());
        uploadBtn.setEnabled(false);
        toast("上传中…");
        pool.execute(() -> {
            try {
                Api.Resp r = Api.upload(token, when, arr);
                ui.post(() -> {
                    uploadBtn.setEnabled(true);
                    toast(r.ok() ? "上传成功 · 等待 21:40 自动写表" : "上传失败：" + r.json.optString("error", "未知"));
                });
            } catch (Exception e) {
                ui.post(() -> {
                    uploadBtn.setEnabled(true);
                    toast("上传失败：" + e.getMessage());
                });
            }
        });
    }

    private void showAbsentList() {
        StringBuilder sb = new StringBuilder();
        int n = 0;
        for (Room r : rooms) {
            Set<Integer> s = absent.get(r.room);
            if (s == null) continue;
            for (int i : s) {
                n++;
                sb.append(r.room).append(" · ").append(i + 1).append(" 号铺 · ")
                        .append(r.beds.get(i)).append("\n");
            }
        }
        new MaterialAlertDialogBuilder(this)
                .setTitle("缺勤名单（" + n + "）")
                .setMessage(n == 0 ? "还没有人缺勤" : sb.toString().trim())
                .setPositiveButton("好", null)
                .show();
    }

    private void confirmReset() {
        new MaterialAlertDialogBuilder(this)
                .setTitle("重置本次点名")
                .setMessage("清空本层所有标记？")
                .setPositiveButton("重置", (d, w) -> {
                    absent.clear();
                    marks.clear();
                    refreshList();
                })
                .setNegativeButton("取消", null)
                .show();
    }

    /**
     * 写表：常态下直接写（高频操作，少一次点击）；
     * 只有"一个人都没标缺勤"时才拦一下——那多半是误触。
     */
    private void confirmWrite() {
        int n = 0;
        for (Set<Integer> s : absent.values()) n += s.size();
        if (n > 0) { doWriteNow(); return; }
        new MaterialAlertDialogBuilder(this)
                .setTitle("本层还没有人缺勤")
                .setMessage("现在写表会把「全班到齐」写进共享表，确定？")
                .setPositiveButton("写入", (d, w) -> doWriteNow())
                .setNegativeButton("取消", null)
                .show();
    }

    private void doWriteNow() {
        toast("正在写入共享表…");
        pool.execute(() -> {
            try {
                Api.Resp r = Api.writeNow(token);
                ui.post(() -> {
                    if (r.ok()) {
                        String t = new SimpleDateFormat("HH:mm", Locale.CHINA).format(new Date());
                        lastWriteNote = "上次写表 " + t;
                        updateStats();
                        toast("已写入共享表 · " + t);
                    } else {
                        toast("写入失败：" + r.json.optString("error", "未知"));
                    }
                });
            } catch (Exception e) {
                ui.post(() -> toast("写入失败：" + e.getMessage()));
            }
        });
    }

    private void requestBack() {
        int n = 0;
        for (Set<Integer> s : absent.values()) n += s.size();
        if (n == 0) { logout(); return; }
        new MaterialAlertDialogBuilder(this)
                .setTitle("还有 " + n + " 人标了缺勤")
                .setMessage("返回选层会丢掉这些标记，确定？")
                .setPositiveButton("返回", (d, w) -> logout())
                .setNegativeButton("继续点名", null)
                .show();
    }

    private void logout() {
        token = "";
        label = "";
        prefs.edit().remove("token").remove("label").apply();
        absent.clear();
        marks.clear();
        showPicker();
    }

    // ---------------- OTA ----------------

    private void checkUpdate(boolean manual) {
        pool.execute(() -> {
            if (manual) ui.post(() -> note("检查中…"));
            try {
                Api.Resp r = Api.appLatest();
                if (!r.ok()) {
                    if (manual) ui.post(() -> note("检查失败：服务不可用"));
                    return;
                }
                int remote = r.json.optInt("versionCode", 0);
                int local = getPackageManager().getPackageInfo(getPackageName(), 0).versionCode;
                lastUpdCheck = System.currentTimeMillis();
                if (remote <= local) {
                    if (manual) ui.post(() -> note("已是最新 v" + versionName()));
                    return;
                }
                if (getPreferences(MODE_PRIVATE).getInt("skip_version", 0) == remote) {
                    if (manual) ui.post(() -> note("已跳过这个版本"));
                    return;
                }
                String ver = r.json.optString("versionName", "?");
                String file = r.json.optString("file", "");
                String sha = r.json.optString("sha256", "");
                ui.post(() -> askUpdate(ver, file, sha, remote));
            } catch (Exception e) {
                if (manual) ui.post(() -> note("检查失败：网络不可用"));
            }
        });
    }

    private void note(String s) {
        if (checkNote != null) checkNote.setText(s);
        else toast(s);
    }

    private void askUpdate(String ver, String file, String sha, int code) {
        new MaterialAlertDialogBuilder(this)
                .setTitle("发现新版本 " + ver)
                .setMessage("「立即更新」马上下载并重启安装；「稍后」下次打开再问。更新不会丢数据。")
                .setPositiveButton("立即更新", (d, w) -> doUpdate(file, sha))
                .setNeutralButton("稍后", null)
                .setNegativeButton("跳过此版本", (d, w) -> {
                    getPreferences(MODE_PRIVATE).edit().putInt("skip_version", code).apply();
                    toast("已跳过 " + ver);
                })
                .show();
    }

    private void doUpdate(String file, String sha) {
        toast("正在下载更新…");
        pool.execute(() -> {
            try {
                Api.Resp r = Api.apkBase64(file);
                if (!r.ok()) throw new Exception("下载失败");
                byte[] apk = Base64.decode(r.json.optString("b64"), Base64.DEFAULT);
                if (sha != null && !sha.isEmpty() && !sha.equalsIgnoreCase(sha256(apk))) {
                    throw new Exception("校验不符");
                }
                File dir = new File(getCacheDir(), "ota");
                if (!dir.exists() && !dir.mkdirs()) throw new Exception("缓存不可写");
                File out = new File(dir, "update.apk");
                try (FileOutputStream fo = new FileOutputStream(out)) {
                    fo.write(apk);
                }
                ui.post(() -> install(out));
            } catch (Exception e) {
                ui.post(() -> toast("更新失败：" + e.getMessage()));
            }
        });
    }

    private void install(File apk) {
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

    private static String sha256(byte[] b) throws Exception {
        MessageDigest md = MessageDigest.getInstance("SHA-256");
        byte[] d = md.digest(b);
        StringBuilder sb = new StringBuilder();
        for (byte x : d) sb.append(String.format("%02x", x));
        return sb.toString();
    }

    // ---------------- 小工具 ----------------

    /** 统一字体：部分国产 ROM 会把系统"个性字体"套到所有 App 上，这里强制无衬线 */
    private void lockFont(View v) {
        if (v instanceof TextView) {
            TextView t = (TextView) v;
            int style = t.getTypeface() == null ? Typeface.NORMAL : t.getTypeface().getStyle();
            t.setTypeface(Typeface.create("sans-serif", style));
        } else if (v instanceof ViewGroup) {
            ViewGroup g = (ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) lockFont(g.getChildAt(i));
        }
    }

    /** Android 15 起强制 edge-to-edge，得自己把系统栏让出来 */
    private void padInsets(View v, boolean top, boolean bottom) {
        final int l = v.getPaddingLeft(), t = v.getPaddingTop();
        final int r = v.getPaddingRight(), b = v.getPaddingBottom();
        ViewCompat.setOnApplyWindowInsetsListener(v, (view, insets) -> {
            Insets bars = insets.getInsets(WindowInsetsCompat.Type.systemBars());
            view.setPadding(l, t + (top ? bars.top : 0), r, b + (bottom ? bars.bottom : 0));
            return insets;
        });
    }

    private int statusBarHeight() {
        int id = getResources().getIdentifier("status_bar_height", "dimen", "android");
        return id > 0 ? getResources().getDimensionPixelSize(id) : dp(24);
    }

    private View statusSpacer() {
        View v = new View(this);
        v.setBackgroundColor(c(R.color.brand_dark));
        v.setLayoutParams(new LinearLayout.LayoutParams(-1, statusBarHeight()));
        return v;
    }

    private int c(int id) { return androidx.core.content.ContextCompat.getColor(this, id); }

    private int dp(int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                getResources().getDisplayMetrics());
    }

    private void toast(String s) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show();
    }

    private String versionName() {
        try {
            return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
        } catch (Exception e) {
            return "?";
        }
    }
}
