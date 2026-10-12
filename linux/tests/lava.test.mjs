// The live badge's lava indicator (linux/gnome-extension/lava.js): run with `node --test linux/tests/lava.test.mjs`.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';

// lava.js is an ES module in a plain .js file, as GNOME Shell wants it; Node before 22 reads .js as CommonJS, so it is
// imported from its text.
const source = await readFile(new URL('../gnome-extension/lava.js', import.meta.url), 'utf8');
const {HEIGHT, RADIUS, areaWidth, bridge, bridges, countText, drift, easeInOut, merge, mix, rgb, shapeWidth} =
    await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

test('the count and the final shape: a circle for one digit, a wider pill for more, 99+ above 99', () => {
    assert.equal(countText(4), '4');
    assert.equal(countText(100), '99+');
    assert.equal(shapeWidth(4), HEIGHT);
    assert.ok(shapeWidth(12) > shapeWidth(4) && shapeWidth(150) > shapeWidth(12));
    for (const n of [1, 9, 12, 99, 150])
        assert.ok(areaWidth(n) >= shapeWidth(n) && areaWidth(n) >= 24, n);
});

test('the drifting blobs stay inside the badge for a long check', () => {
    for (const width of [24, areaWidth(12)]) {
        for (let ms = 0; ms < 120000; ms += 37) {
            for (const b of drift(ms, width)) {
                assert.ok(b.r > 1.5, `radius at ${ms} ms`);
                assert.ok(b.x - b.r >= 0 && b.x + b.r <= width, `x at ${ms} ms`);
                assert.ok(b.y - b.r >= 0 && b.y + b.r <= HEIGHT, `y at ${ms} ms`);
            }
        }
    }
});

test('the blobs flow into each other and apart again (bridges come and go)', () => {
    const seen = new Set();
    for (let ms = 0; ms < 20000; ms += 50)
        seen.add(bridges(drift(ms)).length);
    assert.ok(seen.has(0) && [...seen].some(n => n >= 1), [...seen]);
});

test('a bridge only between close blobs, never when one holds the other', () => {
    assert.equal(bridge({x: 0, y: 0, r: 3}, {x: 30, y: 0, r: 3}), null);
    assert.equal(bridge({x: 0, y: 0, r: 6}, {x: 1, y: 0, r: 2}), null);
    const b = bridge({x: 0, y: 0, r: 3}, {x: 7, y: 0, r: 3});
    for (const p of Object.values(b))
        assert.ok(p.every(Number.isFinite), JSON.stringify(b));
});

test('merging ends in the final shape: one circle, or a pill with the blobs at its ends and centre', () => {
    const start = drift(1234);
    const one = merge(start, 1, 4);
    for (const b of one)
        assert.deepEqual([b.x, b.y, b.r], [12, HEIGHT / 2, RADIUS]);
    const width = areaWidth(12), pill = merge(drift(1234, width), 1, 12, width);
    const xs = pill.map(b => b.x).sort((a, b) => a - b);
    assert.deepEqual(xs, [width / 2 - (shapeWidth(12) - HEIGHT) / 2, width / 2, width / 2 + (shapeWidth(12) - HEIGHT) / 2]);
    assert.deepEqual(merge(start, 0, 4), start);
    assert.equal(easeInOut(-1), 0);
    assert.equal(easeInOut(2), 1);
});

test('lavender to brick', () => {
    const lavender = rgb('#66579E'), brick = rgb('#AD4E2C');
    assert.deepEqual(mix(lavender, brick, 0), lavender);
    assert.deepEqual(mix(lavender, brick, 1), brick);
    assert.deepEqual(mix(lavender, brick, 5), brick);
});
