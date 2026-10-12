// GramaDrama: places in the correction panel's text (branding/PANEL.md, Edycja). Offsets are code points, as
// ClutterText positions and the engine count them; Pango counts UTF-8 bytes. Pure functions, no GNOME imports:
// panel.js uses them, linux/tests/textmap.test.mjs tests them.

const encoder = new TextEncoder();

/** The UTF-8 byte index of a code point offset in text. */
export function byteIndex(text, offset) {
    return encoder.encode(Array.from(text).slice(0, Math.max(0, offset)).join('')).length;
}

/** The code point offset of a UTF-8 byte index in text (a byte inside a character counts as that character). */
export function codeOffset(text, byte) {
    let bytes = 0, offset = 0;
    for (const ch of text) {
        if (bytes >= byte)
            break;
        bytes += encoder.encode(ch).length;
        offset++;
    }
    return offset;
}

/** Code point offsets where the lines of a Pango layout start (its lines' start_index count UTF-8 bytes). */
export function lineStarts(text, byteStarts) {
    const out = [];
    for (const byte of byteStarts) {
        const offset = codeOffset(text, byte);
        if (!out.length || offset > out[out.length - 1])
            out.push(offset);
    }
    return out;
}
