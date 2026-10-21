package io.github.unknownprocess.wristle;

/**
 * "Wristle Dictate": opens straight into recording (locked on), tap to send,
 * then closes back to the watch face. Meant for the App shortcut complication.
 */
public class DictateActivity extends MainActivity {
    @Override
    boolean quickMode() {
        return true;
    }
}
