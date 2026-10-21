package io.github.unknownprocess.wristle;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Context;
import android.content.DialogInterface;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.LayerDrawable;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.os.Vibrator;
import android.text.InputType;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.WindowManager;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.BufferedOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.IOException;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.LinkedBlockingQueue;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Wristle: hold (or tap) the big button and the watch streams its microphone
 * over Wi-Fi to the Wristle desktop listener, which feeds your dictation app.
 */
public class MainActivity extends Activity {

    static final int PROTOCOL = 2;                 // 2 = HMAC challenge-response, see handleStatus
    static final int TCP_PORT = 47820;
    static final int UDP_PORT = 47821;
    static final int RATE = 16000;
    static final int CHUNK_BYTES = 640;            // 20 ms of 16 kHz PCM16
    static final long TAP_MS = 350;                // shorter press = tap (toggle mode)
    static final long MAX_SESSION_MS = 5 * 60 * 1000;
    static final long PING_MS = 10000;

    static final byte[] END = new byte[0];
    static final byte[] CANCEL = new byte[0];

    static final int COLOR_IDLE = 0xFF3A3A3C;
    static final int COLOR_READY = 0xFF14B8A6;
    static final int COLOR_REC = 0xFFE5383B;

    final Handler ui = new Handler(Looper.getMainLooper());

    TextView button;
    TextView status;
    TextView sub;
    TextView cancelBtn;
    GradientDrawable circle;

    SharedPreferences prefs;
    String deviceId;
    volatile String pairCode;

    ConnectivityManager cm;
    ConnectivityManager.NetworkCallback netCallback;
    volatile Network wifi;

    final Object connSignal = new Object();
    final Object writeLock = new Object();
    volatile Socket socket;
    volatile DataOutputStream out;
    volatile boolean approved;
    volatile boolean denied;
    volatile boolean running;
    volatile String pcName;
    volatile String connectError;
    volatile int connGen;   // bumped on every onStart/onStop so a stale connection thread exits

    volatile boolean recording;
    boolean toggleMode;
    boolean downStoppedSession;
    long pressAt;
    long sessionStart;
    LinkedBlockingQueue<byte[]> queue;

    // ------------------------------------------------------------------ UI

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("wristle", MODE_PRIVATE);
        deviceId = prefs.getString("deviceId", null);
        if (deviceId == null) {
            deviceId = UUID.randomUUID().toString().replace("-", "");
            prefs.edit().putString("deviceId", deviceId).apply();
        }
        cm = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        buildUi();
    }

    void buildUi() {
        int w = getResources().getDisplayMetrics().widthPixels;
        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);

        LinearLayout col = new LinearLayout(this);
        col.setOrientation(LinearLayout.VERTICAL);
        col.setGravity(Gravity.CENTER_HORIZONTAL);

        status = text(15, Color.WHITE, Typeface.DEFAULT_BOLD);
        status.setText("Starting…");
        status.setPadding(w / 8, 0, w / 8, dp(4));
        status.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { showSettings(); }
        });

        circle = new GradientDrawable();
        circle.setShape(GradientDrawable.OVAL);
        circle.setColor(COLOR_IDLE);
        int size = (int) (w * 0.46f);
        button = text(34, Color.WHITE, Typeface.DEFAULT_BOLD);
        button.setContentDescription("Hold to talk");
        // Coloured circle with the same white mic glyph as the app icon on top.
        Drawable mic = getDrawable(R.drawable.ic_mic);
        LayerDrawable face = new LayerDrawable(new Drawable[]{circle, mic});
        face.setLayerSize(1, (int) (size * 0.42f), (int) (size * 0.42f));
        face.setLayerGravity(1, Gravity.CENTER);
        button.setBackground(face);
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(size, size);
        bp.gravity = Gravity.CENTER_HORIZONTAL;
        button.setLayoutParams(bp);
        button.setOnTouchListener(new View.OnTouchListener() {
            @Override public boolean onTouch(View v, MotionEvent e) { return onButtonTouch(e); }
        });

        sub = text(12, 0xFFAAAAAA, Typeface.DEFAULT);
        sub.setText("Hold to talk · tap to lock");
        sub.setPadding(w / 8, dp(6), w / 8, 0);

        cancelBtn = text(13, 0xFFFF8A80, Typeface.DEFAULT_BOLD);
        cancelBtn.setText("✕ Cancel");
        cancelBtn.setPadding(dp(12), dp(4), dp(12), dp(4));
        cancelBtn.setVisibility(View.INVISIBLE);
        cancelBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { stopSession(true); }
        });

        col.addView(status);
        col.addView(button);
        col.addView(sub);
        col.addView(cancelBtn);

        FrameLayout.LayoutParams cp = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.CENTER);
        root.addView(col, cp);
        setContentView(root);
    }

    TextView text(float sp, int color, Typeface tf) {
        TextView t = new TextView(this);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        t.setTextColor(color);
        t.setTypeface(tf);
        t.setGravity(Gravity.CENTER);
        return t;
    }

    int dp(int v) {
        return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
    }

    void setStatus(final String main, final String small) {
        ui.post(new Runnable() {
            @Override public void run() {
                status.setText(main);
                if (small != null && !recording) sub.setText(small);
                circle.setColor(recording ? COLOR_REC : (isReady() ? COLOR_READY : COLOR_IDLE));
            }
        });
    }

    void refreshStatus() {
        if (recording) return;
        if (denied) setStatus("Denied on PC", "Reopen Wristle to try again");
        else if (wifi == null) setStatus("Waiting for Wi-Fi…", "Watch must join your home Wi-Fi");
        else if (socket == null) setStatus("Looking for PC…",
                connectError != null ? connectError : "Start Wristle on the PC");
        else if (!approved) setStatus(pairCode != null ? "Approve on PC" : "Connecting…",
                pairCode != null ? "Pairing code " + pairCode : null);
        else setStatus("Ready · " + (pcName != null ? pcName : "PC"), "Hold to talk · tap to lock");
    }

    boolean isReady() {
        return socket != null && approved;
    }

    void showSettings() {
        final EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setHint("auto");
        input.setText(prefs.getString("manualHost", ""));
        new AlertDialog.Builder(this)
                .setTitle("PC address (blank = auto)")
                .setView(input)
                .setPositiveButton("Save", new DialogInterface.OnClickListener() {
                    @Override public void onClick(DialogInterface d, int which) {
                        prefs.edit().putString("manualHost", input.getText().toString().trim()).apply();
                        disconnect();
                        poke();
                    }
                })
                .setNegativeButton("Close", null)
                .show();
    }

    // ------------------------------------------------------------------ lifecycle

    @Override
    protected void onStart() {
        super.onStart();
        running = true;
        denied = false;  // reopening the app is the way to retry after a "no" on the PC
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
        }
        requestWifi();
        final int gen = ++connGen;
        new Thread(new Runnable() {
            @Override public void run() { connectionLoop(gen); }
        }, "wristle-conn").start();
        refreshStatus();
        if (quickMode() && !recording
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            startQuick();
        }
    }

    /** Overridden by DictateActivity: start recording on open, close after sending. */
    boolean quickMode() {
        return false;
    }

    void startQuick() {
        ui.post(new Runnable() {
            @Override public void run() {
                if (recording || !running) return;
                startSession();
                if (recording) {
                    toggleMode = true;
                    sub.setText("Tap to send");
                }
            }
        });
    }

    volatile long holdOpenUntil;

    void closeSoon(long ms) {
        if (!quickMode()) return;
        ui.postDelayed(new Runnable() {
            @Override public void run() {
                long wait = holdOpenUntil - SystemClock.elapsedRealtime();
                if (wait > 0) {
                    closeSoon(wait);
                } else if (!recording && !isFinishing()) {
                    finishAndRemoveTask();
                }
            }
        }, ms);
    }

    @Override
    protected void onStop() {
        if (recording) stopSession(false);
        running = false;
        connGen++;
        poke();
        disconnect();
        if (netCallback != null) {
            try { cm.unregisterNetworkCallback(netCallback); } catch (Exception ignored) { }
            netCallback = null;
        }
        cm.bindProcessToNetwork(null);
        wifi = null;
        super.onStop();
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (results.length > 0 && results[0] != PackageManager.PERMISSION_GRANTED) {
            setStatus("Mic permission needed", "Allow microphone in app settings");
        } else if (results.length > 0 && quickMode()) {
            startQuick();
        }
    }

    // ------------------------------------------------------------------ Wi-Fi

    void requestWifi() {
        NetworkRequest req = new NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .build();
        netCallback = new ConnectivityManager.NetworkCallback() {
            @Override public void onAvailable(Network network) {
                wifi = network;
                cm.bindProcessToNetwork(network);
                poke();
                refreshStatus();
            }

            @Override public void onLost(Network network) {
                if (network.equals(wifi)) {
                    wifi = null;
                    disconnect();
                    refreshStatus();
                }
            }
        };
        try {
            cm.requestNetwork(req, netCallback);
        } catch (Exception e) {
            setStatus("Wi-Fi request failed", e.getMessage());
        }
    }

    // ------------------------------------------------------------------ connection

    void poke() {
        synchronized (connSignal) { connSignal.notifyAll(); }
    }

    void waitSignal(long ms) {
        synchronized (connSignal) {
            try { connSignal.wait(ms); } catch (InterruptedException ignored) { }
        }
    }

    void connectionLoop(int gen) {
        long lastPing = 0;
        while (running && gen == connGen) {
            Network net = wifi;
            if (net == null || denied) {
                waitSignal(2000);
                continue;
            }
            if (socket == null) {
                try {
                    connect(net, gen);
                    connectError = null;
                } catch (Exception e) {
                    connectError = "Can't reach PC (" + e.getClass().getSimpleName() + ")";
                    disconnect();
                    refreshStatus();
                    waitSignal(recording ? 500 : 3000);
                    continue;
                }
            }
            long now = SystemClock.elapsedRealtime();
            if (now - lastPing > PING_MS) {
                try {
                    sendFrame('P', null);
                } catch (IOException e) {
                    disconnect();
                    continue;
                }
                lastPing = now;
            }
            waitSignal(2000);
        }
    }

    void connect(Network net, int gen) throws Exception {
        String host = prefs.getString("manualHost", "");
        int port = TCP_PORT;
        Socket s = null;
        if (host.length() == 0) {
            String last = prefs.getString("lastHost", "");
            if (last.length() > 0) {
                s = tryConnect(net, last, prefs.getInt("lastPort", TCP_PORT), 1200);
            }
            if (s == null) {
                setStatus("Looking for PC…", "Searching your Wi-Fi");
                String[] found = discover(net);
                if (found == null) throw new IOException("not found");
                host = found[0];
                port = Integer.parseInt(found[1]);
                s = tryConnect(net, host, port, 3000);
                if (s == null) throw new IOException("connect failed");
                prefs.edit().putString("lastHost", host).putInt("lastPort", port).apply();
            }
        } else {
            int colon = host.lastIndexOf(':');
            if (colon > 0) {
                port = Integer.parseInt(host.substring(colon + 1));
                host = host.substring(0, colon);
            }
            s = tryConnect(net, host, port, 3000);
            if (s == null) throw new IOException("connect failed");
        }

        if (gen != connGen) {  // the activity stopped/restarted while we were connecting
            try { s.close(); } catch (IOException ignored) { }
            return;
        }
        approved = false;
        pairCode = null;
        socket = s;
        out = new DataOutputStream(new BufferedOutputStream(s.getOutputStream(), 8192));
        final DataInputStream in = new DataInputStream(s.getInputStream());
        final Socket mine = s;
        new Thread(new Runnable() {
            @Override public void run() { readLoop(mine, in); }
        }, "wristle-read").start();

        JSONObject hello = new JSONObject();
        hello.put("id", deviceId);
        hello.put("name", Build.MODEL);
        hello.put("v", PROTOCOL);
        sendFrame('H', hello.toString().getBytes("UTF-8"));
        refreshStatus();
    }

    Socket tryConnect(Network net, String host, int port, int timeoutMs) {
        try {
            Socket s = net.getSocketFactory().createSocket();
            s.connect(new InetSocketAddress(host, port), timeoutMs);
            s.setTcpNoDelay(true);
            return s;
        } catch (Exception e) {
            return null;
        }
    }

    /** UDP broadcast "WRISTLE?" and wait for "WRISTLE! <port> <hostname>". */
    String[] discover(Network net) {
        DatagramSocket ds = null;
        try {
            ds = new DatagramSocket();
            net.bindSocket(ds);
            ds.setBroadcast(true);
            ds.setSoTimeout(800);
            byte[] probe = "WRISTLE?".getBytes("UTF-8");
            InetAddress[] targets = broadcastTargets(net);
            for (int attempt = 0; attempt < 3; attempt++) {
                for (InetAddress t : targets) {
                    try { ds.send(new DatagramPacket(probe, probe.length, t, UDP_PORT)); } catch (IOException ignored) { }
                }
                byte[] buf = new byte[256];
                DatagramPacket p = new DatagramPacket(buf, buf.length);
                try {
                    ds.receive(p);
                } catch (IOException timeout) {
                    continue;
                }
                String reply = new String(p.getData(), 0, p.getLength(), "UTF-8");
                if (reply.startsWith("WRISTLE!")) {
                    String[] parts = reply.split(" ", 3);
                    pcName = parts.length > 2 ? parts[2] : null;
                    return new String[]{p.getAddress().getHostAddress(), parts.length > 1 ? parts[1] : "" + TCP_PORT};
                }
            }
        } catch (Exception ignored) {
        } finally {
            if (ds != null) ds.close();
        }
        return null;
    }

    InetAddress[] broadcastTargets(Network net) throws IOException {
        InetAddress global = InetAddress.getByName("255.255.255.255");
        LinkProperties lp = cm.getLinkProperties(net);
        if (lp != null) {
            for (LinkAddress la : lp.getLinkAddresses()) {
                InetAddress a = la.getAddress();
                int prefix = la.getPrefixLength();
                if (a instanceof Inet4Address && prefix > 0 && prefix < 32) {
                    byte[] b = a.getAddress();
                    int ip = ((b[0] & 0xff) << 24) | ((b[1] & 0xff) << 16) | ((b[2] & 0xff) << 8) | (b[3] & 0xff);
                    int bcast = ip | (int) ((1L << (32 - prefix)) - 1);
                    byte[] bb = {(byte) (bcast >>> 24), (byte) (bcast >>> 16), (byte) (bcast >>> 8), (byte) bcast};
                    return new InetAddress[]{InetAddress.getByAddress(bb), global};
                }
            }
        }
        return new InetAddress[]{global};
    }

    void readLoop(Socket s, DataInputStream in) {
        try {
            while (true) {
                int type = in.readUnsignedByte();
                int len = in.readInt();
                if (len < 0 || len > 65536) throw new IOException("bad frame");
                byte[] payload = new byte[len];
                in.readFully(payload);
                if (type == 'O') handleStatus(new JSONObject(new String(payload, "UTF-8")));
            }
        } catch (Exception e) {
            if (socket == s) {
                disconnect();
                poke();
            }
        }
    }

    /**
     * Pairing: the PC sends "pending" with a code, and after you approve it, "ok" with a
     * random key that we keep per PC. On later connections the PC sends "auth" with a nonce
     * and we prove we hold the key, so the key itself never goes over the network again.
     */
    void handleStatus(JSONObject st) throws Exception {
        String state = st.optString("state");
        String host = st.optString("host", "");
        if (host.length() > 0) pcName = host;
        if ("auth".equals(state)) {
            String key = prefs.getString("key:" + host, null);
            String proof = key != null ? authProof(key, st.optString("nonce"), deviceId) : "";
            sendFrame('U', proof.getBytes("UTF-8"));  // empty = no key: the PC asks to pair again
        } else if ("ok".equals(state)) {
            String key = st.optString("key", "");
            if (key.length() > 0) prefs.edit().putString("key:" + host, key).apply();
            approved = true;
            buzz(30);
            refreshStatus();
        } else if ("pending".equals(state)) {
            approved = false;
            pairCode = st.optString("code", "----");
            refreshStatus();
        } else if ("denied".equals(state)) {
            denied = true;
            approved = false;
            refreshStatus();
        } else if ("busy".equals(state)) {
            setStatus("PC is busy", "Another dictation is running");
        } else if ("done".equals(state)) {
            if (!recording) {
                setStatus("Sent ✓", null);
                ui.postDelayed(new Runnable() {
                    @Override public void run() { refreshStatus(); }
                }, 1200);
                closeSoon(800);
            }
        }
    }

    void sendFrame(char type, byte[] payload) throws IOException {
        DataOutputStream o = out;
        if (o == null) throw new IOException("not connected");
        synchronized (writeLock) {
            o.writeByte(type);
            o.writeInt(payload == null ? 0 : payload.length);
            if (payload != null && payload.length > 0) o.write(payload);
            o.flush();
        }
    }

    void disconnect() {
        Socket s = socket;
        socket = null;
        out = null;
        approved = false;
        if (s != null) {
            try { s.close(); } catch (IOException ignored) { }
        }
    }

    // ------------------------------------------------------------------ dictation

    boolean onButtonTouch(MotionEvent e) {
        switch (e.getActionMasked()) {
            case MotionEvent.ACTION_DOWN:
                downStoppedSession = false;
                if (recording) {
                    // Tap while locked: finish.
                    downStoppedSession = true;
                    stopSession(false);
                } else {
                    pressAt = SystemClock.elapsedRealtime();
                    toggleMode = false;
                    startSession();
                }
                return true;
            case MotionEvent.ACTION_UP:
                if (recording && !downStoppedSession) {
                    if (SystemClock.elapsedRealtime() - pressAt < TAP_MS) {
                        toggleMode = true;
                        sub.setText("Locked · tap to finish");
                    } else {
                        stopSession(false);
                    }
                }
                return true;
            case MotionEvent.ACTION_CANCEL:
                if (recording && !toggleMode) stopSession(false);
                return true;
        }
        return false;
    }

    void startSession() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
            return;
        }
        if (denied) {
            refreshStatus();
            return;
        }
        recording = true;
        sessionStart = SystemClock.elapsedRealtime();
        final LinkedBlockingQueue<byte[]> q = new LinkedBlockingQueue<byte[]>();
        queue = q;
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        cancelBtn.setVisibility(View.VISIBLE);
        sub.setText("Release to send");
        setStatus("Listening…", null);
        buzz(40);
        poke();

        new Thread(new Runnable() {
            @Override public void run() { recordLoop(q); }
        }, "wristle-rec").start();
        new Thread(new Runnable() {
            @Override public void run() { sendLoop(q); }
        }, "wristle-send").start();
    }

    void stopSession(boolean cancel) {
        if (!recording) return;
        recording = false;
        LinkedBlockingQueue<byte[]> q = queue;
        queue = null;
        if (q != null) q.offer(cancel ? CANCEL : END);
        getWindow().clearFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        cancelBtn.setVisibility(View.INVISIBLE);
        button.setScaleX(1f);
        button.setScaleY(1f);
        buzz(cancel ? 80 : 25);
        if (cancel) setStatus("Cancelled", null);
        else setStatus("Sending…", null);
        ui.postDelayed(new Runnable() {
            @Override public void run() { refreshStatus(); }
        }, 3000);
        // Quick mode: close after a cancel, or as a fallback if "done" never arrives.
        closeSoon(cancel ? 700 : 8000);
    }

    /** Keep a quick-mode screen open long enough to read an error. */
    void showError(String main, String small) {
        holdOpenUntil = SystemClock.elapsedRealtime() + 3500;
        setStatus(main, small);
        buzz(200);
    }

    void recordLoop(LinkedBlockingQueue<byte[]> q) {
        AudioRecord rec = null;
        try {
            int min = AudioRecord.getMinBufferSize(RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
            rec = new AudioRecord(MediaRecorder.AudioSource.VOICE_RECOGNITION, RATE,
                    AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT, Math.max(min, RATE * 2));
            if (rec.getState() != AudioRecord.STATE_INITIALIZED) throw new IllegalStateException("mic unavailable");
            rec.startRecording();
            while (recording && queue == q) {
                byte[] b = new byte[CHUNK_BYTES];
                int n = rec.read(b, 0, b.length);
                if (n > 0) {
                    q.offer(n == b.length ? b : java.util.Arrays.copyOf(b, n));
                    showLevel(b, n);
                }
                if (SystemClock.elapsedRealtime() - sessionStart > MAX_SESSION_MS) {
                    ui.post(new Runnable() {
                        @Override public void run() { stopSession(false); }
                    });
                    break;
                }
            }
        } catch (Exception e) {
            final String msg = e.getMessage();
            ui.post(new Runnable() {
                @Override public void run() {
                    stopSession(true);
                    showError("Mic error", msg);
                }
            });
        } finally {
            if (rec != null) {
                try { rec.stop(); } catch (Exception ignored) { }
                rec.release();
            }
        }
    }

    long lastLevelPost;

    void showLevel(byte[] b, int n) {
        long now = SystemClock.elapsedRealtime();
        if (now - lastLevelPost < 60) return;
        lastLevelPost = now;
        double sum = 0;
        for (int i = 0; i + 1 < n; i += 2) {
            int s = (short) ((b[i] & 0xff) | (b[i + 1] << 8));
            sum += (double) s * s;
        }
        double rms = Math.sqrt(sum / Math.max(1, n / 2)) / 32768.0;
        final float scale = 1f + (float) Math.min(0.18, rms * 2.5);
        ui.post(new Runnable() {
            @Override public void run() {
                if (recording) {
                    button.setScaleX(scale);
                    button.setScaleY(scale);
                }
            }
        });
    }

    void sendLoop(LinkedBlockingQueue<byte[]> q) {
        // Audio is buffered in q while we (re)connect, so nothing said is lost.
        long deadline = SystemClock.elapsedRealtime() + 12000;
        while (!isReady() && SystemClock.elapsedRealtime() < deadline && !denied) {
            poke();
            SystemClock.sleep(50);
        }
        if (!isReady()) {
            // Stop right away instead of letting the user keep talking into nothing.
            ui.post(new Runnable() {
                @Override public void run() {
                    if (recording) stopSession(true);
                    showError("PC not reachable", "Is the listener running?");
                }
            });
            drainUntilMarker(q);
            return;
        }
        try {
            sendFrame('S', null);
            while (true) {
                byte[] b = q.take();
                if (b == END) { sendFrame('E', null); break; }
                if (b == CANCEL) { sendFrame('C', null); break; }
                sendFrame('A', b);
            }
        } catch (Exception e) {
            disconnect();
            poke();
            ui.post(new Runnable() {
                @Override public void run() {
                    if (recording) stopSession(true);
                    showError("Connection lost", "Try again");
                }
            });
        }
    }

    void drainUntilMarker(LinkedBlockingQueue<byte[]> q) {
        while (true) {
            byte[] b = q.poll();
            if (b == null) {
                if (!recording) return;
                SystemClock.sleep(20);
                continue;
            }
            if (b == END || b == CANCEL) return;
        }
    }

    // ------------------------------------------------------------------ helpers

    @SuppressWarnings("deprecation")
    void buzz(long ms) {
        try {
            Vibrator v = (Vibrator) getSystemService(Context.VIBRATOR_SERVICE);
            if (v != null) v.vibrate(ms);
        } catch (Exception ignored) { }
    }

    /** HMAC-SHA256(key, "wristle-auth|nonce|id") as hex; the PC computes the same. */
    static String authProof(String keyHex, String nonce, String id) throws Exception {
        byte[] key = new byte[keyHex.length() / 2];
        for (int i = 0; i < key.length; i++) {
            key[i] = (byte) Integer.parseInt(keyHex.substring(2 * i, 2 * i + 2), 16);
        }
        Mac mac = Mac.getInstance("HmacSHA256");
        mac.init(new SecretKeySpec(key, "HmacSHA256"));
        byte[] d = mac.doFinal(("wristle-auth|" + nonce + "|" + id).getBytes("UTF-8"));
        StringBuilder sb = new StringBuilder();
        for (byte b : d) sb.append(String.format(Locale.US, "%02x", b & 0xff));
        return sb.toString();
    }
}
