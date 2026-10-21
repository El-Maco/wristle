package io.github.unknownprocess.wristle;

import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.graphics.drawable.Icon;
import android.os.Binder;
import android.os.Bundle;
import android.os.IBinder;
import android.os.Parcel;
import android.os.RemoteException;
import android.support.wearable.complications.ComplicationText;
import android.util.Log;

/**
 * Watch face complication "Wristle": a mic button that opens DictateActivity.
 *
 * Implements the Wear OS complication provider binder protocol directly
 * (IComplicationProvider / IComplicationManager), so no AndroidX library is needed.
 */
public class DictateComplicationService extends Service {
    static final String TAG = "WristleComplication";
    static final String PROVIDER = "android.support.wearable.complications.IComplicationProvider";
    static final String MANAGER = "android.support.wearable.complications.IComplicationManager";
    static final int API_VERSION = 4;

    // Wire types
    static final int TYPE_NO_DATA = 10;
    static final int TYPE_SHORT_TEXT = 3;
    static final int TYPE_LONG_TEXT = 4;
    static final int TYPE_ICON = 6;
    static final int TYPE_SMALL_IMAGE = 7;
    static final int IMAGE_STYLE_ICON = 2;

    // IComplicationProvider transaction codes (FIRST_CALL_TRANSACTION + aidl id)
    static final int ON_UPDATE = 1;
    static final int ON_DEACTIVATED = 2;
    static final int ON_ACTIVATED = 3;
    static final int GET_API_VERSION = 4;
    static final int GET_PREVIEW = 5;
    static final int ON_START_SYNC = 6;
    static final int ON_STOP_SYNC = 7;
    static final int ON_SYNC_REQUEST = 8;
    static final int ON_UPDATE2 = 9;
    static final int ON_SYNC_REQUEST2 = 10;

    final Binder binder = new Binder() {
        {
            attachInterface(null, PROVIDER);
        }

        @Override
        protected boolean onTransact(int code, Parcel data, Parcel reply, int flags) throws RemoteException {
            if (code == INTERFACE_TRANSACTION) {
                if (reply != null) reply.writeString(PROVIDER);
                return true;
            }
            if (code < ON_UPDATE || code > ON_SYNC_REQUEST2) {
                return super.onTransact(code, data, reply, flags);
            }
            data.enforceInterface(PROVIDER);
            try {
                switch (code) {
                    case ON_UPDATE:
                    case ON_UPDATE2: {
                        int id = data.readInt();
                        int type = data.readInt();
                        IBinder manager = data.readStrongBinder();
                        sendUpdate(manager, id, type);
                        ok(reply);
                        return true;
                    }
                    case ON_ACTIVATED:
                    case ON_DEACTIVATED:
                    case ON_START_SYNC:
                    case ON_STOP_SYNC:
                        ok(reply);
                        return true;
                    case GET_API_VERSION:
                        ok(reply);
                        if (reply != null) reply.writeInt(API_VERSION);
                        return true;
                    case GET_PREVIEW: {
                        int type = data.readInt();
                        ok(reply);
                        writeTyped(reply, type);
                        return true;
                    }
                    case ON_SYNC_REQUEST:
                    case ON_SYNC_REQUEST2: {
                        data.readInt(); // instance id
                        int type = data.readInt();
                        ok(reply);
                        writeTyped(reply, type);
                        return true;
                    }
                }
            } catch (RuntimeException e) {
                Log.w(TAG, "complication call " + code + " failed", e);
                if (reply != null) {
                    reply.setDataPosition(0);
                    reply.writeException(e instanceof IllegalStateException ? e : new IllegalStateException(e));
                }
                return true;
            }
            return super.onTransact(code, data, reply, flags);
        }
    };

    @Override
    public IBinder onBind(Intent intent) {
        return binder;
    }

    static void ok(Parcel reply) {
        if (reply != null) reply.writeNoException();
    }

    void sendUpdate(IBinder manager, int id, int type) {
        Parcel d = Parcel.obtain();
        Parcel r = Parcel.obtain();
        try {
            d.writeInterfaceToken(MANAGER);
            d.writeInt(id);
            writeTyped(d, type);
            manager.transact(IBinder.FIRST_CALL_TRANSACTION, d, r, 0);
            r.readException();
        } catch (Exception e) {
            Log.w(TAG, "updateComplicationData failed", e);
        } finally {
            d.recycle();
            r.recycle();
        }
    }

    /** Writes a nullable ComplicationData the way AIDL does: 1 + (type, fields bundle). */
    void writeTyped(Parcel p, int requestedType) {
        if (p == null) return;
        p.writeInt(1);
        Bundle fields = new Bundle();
        int type = requestedType;
        PendingIntent tap = PendingIntent.getActivity(this, 0,
                new Intent(this, DictateActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
        Icon mono = Icon.createWithResource(this, R.drawable.ic_complication);
        Icon color = Icon.createWithResource(this, R.mipmap.ic_dictate);
        switch (requestedType) {
            case TYPE_SHORT_TEXT:  // not advertised any more; kept for slots set up with older builds
                fields.putParcelable("SHORT_TEXT", new ComplicationText("Dictate"));
                fields.putParcelable("ICON", mono);
                break;
            case TYPE_LONG_TEXT:
                fields.putParcelable("LONG_TITLE", new ComplicationText("Wristle"));
                fields.putParcelable("LONG_TEXT", new ComplicationText("Tap to dictate"));
                fields.putParcelable("ICON", mono);
                break;
            case TYPE_ICON:
                fields.putParcelable("ICON", mono);
                break;
            case TYPE_SMALL_IMAGE:
                fields.putParcelable("SMALL_IMAGE", color);
                fields.putInt("IMAGE_STYLE", IMAGE_STYLE_ICON);
                break;
            default:
                type = TYPE_NO_DATA;
        }
        if (type != TYPE_NO_DATA) {
            fields.putParcelable("TAP_ACTION", tap);
            fields.putParcelable("IMAGE_CONTENT_DESCRIPTION", new ComplicationText("Start dictation"));
        }
        p.writeInt(type);
        p.writeBundle(fields);
    }
}
