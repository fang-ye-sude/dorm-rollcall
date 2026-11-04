package com.example.dormroll;

import android.annotation.SuppressLint;
import android.content.Context;
import android.os.Handler;
import android.os.Looper;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.util.HashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * 网络传输层：借系统 WebView（Chromium）发请求。
 * 裸 TLS 走不通云盾，Chromium 的指纹能过；同时省掉打包 OkHttp。
 */
public class WebTransport implements Api.Transport {

    public interface Ready { void onReady(boolean ok); }

    private final WebView web;
    private final Map<String, Object[]> pending = new HashMap<>();
    private final Handler ui = new Handler(Looper.getMainLooper());
    private volatile boolean ready = false;

    @SuppressLint("SetJavaScriptEnabled")
    public WebTransport(Context ctx, Ready cb) {
        web = new WebView(ctx);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setCacheMode(WebSettings.LOAD_DEFAULT);
        web.addJavascriptInterface(new Bridge(), "RCBridge");
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView v, String url) {
                ready = true;
                if (cb != null) cb.onReady(true);
            }
        });
        // 先落到同源页面，之后 fetch 才是同源请求
        web.loadUrl(Api.BASE + "/manifest");
        Api.transport = this;
    }

    public boolean isReady() { return ready; }

    @Override
    public Api.Resp call(String method, String path, String token, String body, int timeoutMs) throws Exception {
        final String id = UUID.randomUUID().toString().replace("-", "");
        final Object[] box = new Object[1];
        final CountDownLatch latch = new CountDownLatch(1);
        pending.put(id, new Object[]{box, latch});

        StringBuilder js = new StringBuilder();
        js.append("(async()=>{let last;for(let i=0;i<3;i++){try{")
          .append("const ac=new AbortController();const tm=setTimeout(()=>ac.abort(),15000);")
          .append("const r=await fetch(").append(JSONObject.quote(path))
          .append(",{method:").append(JSONObject.quote(method))
          .append(",signal:ac.signal,headers:{'Content-Type':'application/json'");
        if (token != null && !token.isEmpty()) {
            js.append(",'Authorization':").append(JSONObject.quote("Bearer " + token));
        }
        js.append("}");
        if (body != null && !body.isEmpty()) {
            js.append(",body:").append(JSONObject.quote(body));
        }
        js.append("});clearTimeout(tm);")
          .append("const t=await r.text();RCBridge.res(").append(JSONObject.quote(id))
          .append(",r.status,t);return;")
          .append("}catch(e){last=e;await new Promise(x=>setTimeout(x,400*(i+1)));}}")
          .append("RCBridge.err(").append(JSONObject.quote(id)).append(",String(last));})()");

        ui.post(() -> web.evaluateJavascript(js.toString(), null));
        boolean done = latch.await(Math.max(3000, timeoutMs), TimeUnit.MILLISECONDS);
        pending.remove(id);
        if (!done) throw new Exception("请求超时");

        Object got = box[0];
        if (got == null) throw new Exception("网络错误");
        Object[] pair = (Object[]) got;
        int code = (Integer) pair[0];
        String text = String.valueOf(pair[1]);
        JSONObject json;
        try {
            json = new JSONObject(text);
        } catch (Exception e) {
            json = new JSONObject();
        }
        return new Api.Resp(code, json);
    }

    public void destroy() {
        try {
            web.destroy();
        } catch (Exception ignored) {
        }
    }

    public class Bridge {
        @JavascriptInterface
        public void res(String id, int code, String text) {
            Object[] arr = pending.remove(id);
            if (arr != null) {
                ((Object[]) arr[0])[0] = new Object[]{code, text};
                ((CountDownLatch) arr[1]).countDown();
            }
        }

        @JavascriptInterface
        public void err(String id, String msg) {
            Object[] arr = pending.remove(id);
            if (arr != null) {
                ((Object[]) arr[0])[0] = msg;
                ((CountDownLatch) arr[1]).countDown();
            }
        }
    }
}
