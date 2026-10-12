// Places in the correction panel's text (linux/gnome-extension/textmap.js): run with
// `node --test linux/tests/textmap.test.mjs`.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

// an ES module in a plain .js file, as GNOME Shell wants it: imported from its text (see lava.test.mjs)
const source = await readFile(new URL('../gnome-extension/textmap.js', import.meta.url), 'utf8');
const {byteIndex, codeOffset, lineStarts} =
    await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('bytes and code points', () => {
    const text = 'zażółć 😀 x';
    assert.equal(byteIndex(text, 3), 4); // "z", "a", "ż" (2 bytes)
    assert.equal(codeOffset(text, 4), 3);
    assert.equal(codeOffset(text, byteIndex(text, 9)), 9);
    assert.equal(byteIndex(text, 999), new TextEncoder().encode(text).length);
    assert.deepEqual(lineStarts('ąb\ncd', [0, 4]), [0, 3]); // "ą" is two bytes
    assert.deepEqual(lineStarts('ab', [0, 0, 2]), [0, 2]);
});
