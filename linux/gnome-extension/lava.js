// GramaDrama: the geometry of the live badge's "lava" indicator (branding/PANEL.md). While the check
// runs, three lavender blobs drift and flow into each other like wax in a lava lamp; with the result
// they merge into one shape, a circle (a pill for two digits), that turns brick and shows the count.
// Pure functions, no GNOME imports: live.js draws them with Cairo, linux/tests/lava.test.mjs tests them.
// Coordinates are in px from the drawing area's top left corner.

export const HEIGHT = 18;
export const RADIUS = HEIGHT / 2; // of the final circle, and of a pill's ends
export const MERGE_MS = 520; // the blobs flow together
export const TINT_MS = 200; // lavender turns brick
export const NUMBER_MS = 240; // the count pops in
export const FADE_MS = 450; // no issues: the merged blob fades away
const DRIFT = 1.15; // radians per second of the blobs' slowest motion
const BLOBS = 3;

/** The text in the badge: the count, "99+" above 99. */
export function countText(count) {
    return count > 99 ? '99+' : String(count);
}

/** Width of the final shape: a circle for one digit, a pill for more. */
export function shapeWidth(count) {
    const digits = countText(count).length;
    return digits <= 1 ? HEIGHT : HEIGHT + 6 * (digits - 1);
}

/** Width of the drawing area: room for the drifting blobs and for the final shape. */
export function areaWidth(count) {
    return Math.max(24, shapeWidth(count));
}

/** The blobs at `ms` since the check started: [{x, y, r}], always inside an area of `width` x HEIGHT. */
export function drift(ms, width = 24) {
    const t = ms / 1000 * DRIFT;
    const out = [];
    for (let i = 0; i < BLOBS; i++) {
        const p = t + i * 2.1;
        out.push({
            x: width / 2 + 6.2 * Math.sin(p * 1.3 + i),
            y: HEIGHT / 2 + 2.6 * Math.sin(p * 1.9 + i * 1.7),
            r: 2.5 + 0.7 * Math.sin(p * 1.1 + i * 2.4),
        });
    }
    return out;
}

export function easeInOut(p) {
    p = Math.min(1, Math.max(0, p));
    return p < 0.5 ? 2 * p * p : 1 - (-2 * p + 2) ** 2 / 2;
}

/** The blobs part of the way (`progress` 0..1) from `blobs` to the final shape for `count`: the middle blob to
 *  the centre, the outer ones to a pill's ends, every radius to RADIUS. */
export function merge(blobs, progress, count, width = 24) {
    const e = easeInOut(progress);
    const half = (shapeWidth(count) - HEIGHT) / 2; // from the centre to a pill end's centre
    const order = blobs.map((b, i) => [b.x, i]).sort((a, b) => a[0] - b[0]).map(([, i]) => i);
    return blobs.map((b, i) => {
        const slot = order.indexOf(i) - 1; // left, middle, right
        const x = width / 2 + slot * half, y = HEIGHT / 2;
        return {x: b.x + (x - b.x) * e, y: b.y + (y - b.y) * e, r: b.r + (RADIUS - b.r) * e};
    });
}

const vec = (x, y, angle, r) => [x + r * Math.cos(angle), y + r * Math.sin(angle)];

/** The bridge that makes two close blobs flow into each other (the 2D "metaball" outline): four points on the
 *  circles and four Bézier handles, or null when they are too far apart or one holds the other. Drawn as
 *  p1 →(h1, h3)→ p3 → p4 →(h4, h2)→ p2, closed, and filled together with both circles. */
export function bridge(a, b, spread = 0.5, handle = 2.4) {
    const d = Math.hypot(b.x - a.x, b.y - a.y);
    if (a.r <= 0 || b.r <= 0 || d > a.r + b.r * 2.5 || d <= Math.abs(a.r - b.r))
        return null;
    let u1 = 0, u2 = 0;
    if (d < a.r + b.r) {
        u1 = Math.acos((a.r * a.r + d * d - b.r * b.r) / (2 * a.r * d));
        u2 = Math.acos((b.r * b.r + d * d - a.r * a.r) / (2 * b.r * d));
    }
    const between = Math.atan2(b.y - a.y, b.x - a.x);
    const max = Math.acos((a.r - b.r) / d);
    const a1 = between + u1 + (max - u1) * spread, a2 = between - u1 - (max - u1) * spread;
    const a3 = between + Math.PI - u2 - (Math.PI - u2 - max) * spread;
    const a4 = between - Math.PI + u2 + (Math.PI - u2 - max) * spread;
    const p1 = vec(a.x, a.y, a1, a.r), p2 = vec(a.x, a.y, a2, a.r);
    const p3 = vec(b.x, b.y, a3, b.r), p4 = vec(b.x, b.y, a4, b.r);
    const total = a.r + b.r;
    const k = Math.min(spread * handle, Math.hypot(p1[0] - p3[0], p1[1] - p3[1]) / total) * Math.min(1, d * 2 / total);
    return {
        p1, p2, p3, p4,
        h1: vec(p1[0], p1[1], a1 - Math.PI / 2, a.r * k), h2: vec(p2[0], p2[1], a2 + Math.PI / 2, a.r * k),
        h3: vec(p3[0], p3[1], a3 + Math.PI / 2, b.r * k), h4: vec(p4[0], p4[1], a4 - Math.PI / 2, b.r * k),
    };
}

/** Every bridge between the blobs that are close enough. */
export function bridges(blobs) {
    const out = [];
    for (let i = 0; i < blobs.length; i++) {
        for (let j = i + 1; j < blobs.length; j++) {
            const b = bridge(blobs[i], blobs[j]);
            if (b)
                out.push(b);
        }
    }
    return out;
}

/** '#RRGGBB' as [r, g, b] in 0..1. */
export function rgb(hex) {
    return [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
}

/** The colour `f` (0..1) of the way from `a` to `b`, both [r, g, b]. */
export function mix(a, b, f) {
    f = Math.min(1, Math.max(0, f));
    return a.map((v, i) => v + (b[i] - v) * f);
}
