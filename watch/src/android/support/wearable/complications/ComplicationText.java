package android.support.wearable.complications;

import android.os.Bundle;
import android.os.Parcel;
import android.os.Parcelable;

/**
 * Minimal wire-compatible stand-in for the Wear complication text class.
 * Watch faces unparcel it with their own copy of the class (matched by name),
 * so only the parcel layout has to agree: a Bundle with "surrounding_string".
 */
public final class ComplicationText implements Parcelable {
    private final CharSequence text;

    public ComplicationText(CharSequence text) {
        this.text = text;
    }

    @Override
    public int describeContents() {
        return 0;
    }

    @Override
    public void writeToParcel(Parcel out, int flags) {
        Bundle b = new Bundle();
        b.putCharSequence("surrounding_string", text);
        out.writeBundle(b);
    }

    public static final Creator<ComplicationText> CREATOR = new Creator<ComplicationText>() {
        @Override
        public ComplicationText createFromParcel(Parcel in) {
            Bundle b = in.readBundle(ComplicationText.class.getClassLoader());
            return new ComplicationText(b == null ? "" : b.getCharSequence("surrounding_string"));
        }

        @Override
        public ComplicationText[] newArray(int size) {
            return new ComplicationText[size];
        }
    };
}
