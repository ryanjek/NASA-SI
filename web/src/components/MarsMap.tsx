import { useEffect, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent as RPointerEvent } from 'react';
import { markerLabel, regionName, type MarsSite } from '../data/marsSites';

const MAX_ZOOM = 4;
const POPUP_W = 380 + 24; // .popup width + right margin, see styles.css
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));

function rng(seed: number) {
  let s = seed;
  return () => (s = (s * 16807) % 2147483647) / 2147483647;
}

interface Props {
  sites: readonly MarsSite[];
  selectedId: string | null;
  scoreOf: (s: MarsSite) => number | null;
  onSelect: (id: string) => void;
}

type Pt = [number, number];
type View = { x: number; y: number; w: number; h: number };

/** Equirectangular projection around the sites' mean latitude. Units are arbitrary; `fit` scales them to pixels. */
function project(sites: readonly MarsSite[]): Pt[] {
  const mid = sites.reduce((a, s) => a + s.lat, 0) / (sites.length || 1);
  const c = Math.cos((mid * Math.PI) / 180);
  return sites.map((s) => [s.lon * c, -s.lat]);
}

/** Uniform scale + offset that places the projected points inside a pixel rectangle. */
function fit(pts: Pt[], r: { x0: number; y0: number; x1: number; y1: number }) {
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const w = Math.max(maxX - minX, 1e-6);
  const h = Math.max(maxY - minY, 1e-6);
  const s = Math.max(0.01, Math.min((r.x1 - r.x0) / w, (r.y1 - r.y0) / h));
  const ox = (r.x0 + r.x1) / 2 - ((minX + maxX) / 2) * s;
  const oy = (r.y0 + r.y1) / 2 - ((minY + maxY) / 2) * s;
  return { s, ox, oy };
}

/** Convex hull (monotone chain). */
function hull(points: Pt[]): Pt[] {
  const p = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (p.length < 3) return p;
  const cross = (o: Pt, a: Pt, b: Pt) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower: Pt[] = [];
  for (const q of p) {
    while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], q) <= 0) lower.pop();
    lower.push(q);
  }
  const upper: Pt[] = [];
  for (const q of [...p].reverse()) {
    while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], q) <= 0) upper.pop();
    upper.push(q);
  }
  return [...lower.slice(0, -1), ...upper.slice(0, -1)];
}

/** Closed Catmull-Rom spline through the hull points, as an SVG path. */
function smoothPath(pts: Pt[]): string {
  const n = pts.length;
  let d = `M${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 0; i < n; i++) {
    const [p0, p1, p2, p3] = [pts[(i - 1 + n) % n], pts[i], pts[(i + 1) % n], pts[(i + 2) % n]];
    const c1: Pt = [p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6];
    const c2: Pt = [p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6];
    d += `C${c1[0].toFixed(1)} ${c1[1].toFixed(1)} ${c2[0].toFixed(1)} ${c2[1].toFixed(1)} ${p2[0].toFixed(1)} ${p2[1].toFixed(1)}`;
  }
  return d + 'Z';
}

/**
 * Mars candidate sites placed from their coordinates over illustrative terrain (random blobs, not real topography).
 * The view always fits every site inside the visible area. Wheel / trackpad scroll and pinch zoom around the cursor
 * (1x to MAX_ZOOM), drag pans while zoomed, double-click resets.
 */
export function MarsMap({ sites, selectedId, scoreOf, onSelect }: Props) {
  const svgRef = useRef<SVGSVGElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const sizeRef = useRef(size);
  // `null` = 1x (whole frame). Otherwise the zoomed window, in drawing units (1 unit = 1 CSS px at 1x).
  const [vb, setVb] = useState<View | null>(null);
  const vbRef = useRef<View | null>(null);
  const drag = useRef<{ x: number; y: number; vb: View; moved: boolean; id: number } | null>(null);
  const suppressClick = useRef(false);

  const setView = (v: View | null) => {
    vbRef.current = v;
    setVb(v);
  };
  const full = (): View => ({ x: 0, y: 0, w: sizeRef.current.w, h: sizeRef.current.h });
  /** Keep a zoomed view inside the 1x frame. */
  const contain = (v: View): View => {
    const f = full();
    return { ...v, x: clamp(v.x, 0, f.w - v.w), y: clamp(v.y, 0, f.h - v.h) };
  };

  // Track the map's pixel size so the drawing space always matches the screen: nothing is ever cropped.
  useLayoutEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const measure = (w: number, h: number) => {
      w = Math.round(w);
      h = Math.round(h);
      if (w === sizeRef.current.w && h === sizeRef.current.h) return;
      sizeRef.current = { w, h };
      setSize({ w, h });
      setView(null); // a resize resets the zoom
    };
    const r = svg.getBoundingClientRect();
    measure(r.width, r.height);
    const ro = new ResizeObserver(([e]) => measure(e.contentRect.width, e.contentRect.height));
    ro.observe(svg);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault(); // the map owns the wheel; the page never scrolls
      const f = full();
      if (!f.w || !f.h) return;
      const cur = vbRef.current ?? f;
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? f.h : 1; // lines / pages -> px
      const dx = e.deltaX * unit;
      const dy = e.deltaY * unit;
      // A sideways trackpad swipe pans (when zoomed) instead of zooming.
      if (!e.ctrlKey && Math.abs(dx) > Math.abs(dy)) {
        if (vbRef.current) setView(contain({ ...cur, x: cur.x + dx * (cur.w / f.w) }));
        return;
      }
      // Pinch arrives as ctrl+wheel with small deltas, so it gets more gain. Big mouse-wheel jumps are capped.
      const step = clamp(dy, -120, 120) * (e.ctrlKey ? 0.01 : 0.0025);
      const w = clamp(cur.w * Math.exp(step), f.w / MAX_ZOOM, f.w);
      if (w === cur.w) return;
      const k = w / cur.w;
      const r = svg.getBoundingClientRect();
      // Cursor position in drawing units; it stays under the cursor.
      const px = cur.x + ((e.clientX - r.left) / r.width) * cur.w;
      const py = cur.y + ((e.clientY - r.top) / r.height) * cur.h;
      if (w >= f.w - 0.5) return setView(null);
      setView(contain({ x: px - (px - cur.x) * k, y: py - (py - cur.y) * k, w, h: cur.h * k }));
    };
    svg.addEventListener('wheel', onWheel, { passive: false });
    return () => svg.removeEventListener('wheel', onWheel);
  }, []);

  const onPointerDown = (e: RPointerEvent<SVGSVGElement>) => {
    suppressClick.current = false;
    if (e.button !== 0 || !vbRef.current) return; // panning only makes sense when zoomed in
    drag.current = { x: e.clientX, y: e.clientY, vb: vbRef.current, moved: false, id: e.pointerId };
  };
  const onPointerMove = (e: RPointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.x;
    const dy = e.clientY - d.y;
    if (!d.moved) {
      if (Math.hypot(dx, dy) < 4) return; // still a click
      d.moved = true;
      try {
        e.currentTarget.setPointerCapture(d.id);
      } catch {
        /* pointer already released; the drag still works while it stays over the map */
      }
    }
    const f = full();
    setView(contain({ ...d.vb, x: d.vb.x - dx * (d.vb.w / f.w), y: d.vb.y - dy * (d.vb.h / f.h) }));
  };
  const endDrag = (e: RPointerEvent<SVGSVGElement>) => {
    const d = drag.current;
    drag.current = null;
    if (!d?.moved) return;
    // The click that ends a drag must not select a site. If no click follows (released elsewhere), clear the flag.
    suppressClick.current = true;
    window.setTimeout(() => (suppressClick.current = false), 0);
    if (e.currentTarget.hasPointerCapture(d.id)) e.currentTarget.releasePointerCapture(d.id);
  };

  const { w: W, h: H } = size;
  const view = vb ?? { x: 0, y: 0, w: W, h: H };
  const zoom = W && view.w ? W / view.w : 1;
  const wide = W > 1000;

  const layout = useMemo(() => {
    if (!W || !H || !sites.length) return null;
    const pts = project(sites);
    // Fit inside the area the overlays leave free: status pill on top, legend bottom-left (wide screens only), tags to the right.
    const pad = { l: wide ? 130 : 125, r: wide ? 170 : 120, t: 150, b: wide ? 230 : 70 };
    const base = fit(pts, { x0: pad.l, y0: pad.t, x1: W - pad.r, y1: H - pad.b });
    const pos = pts.map(([x, y]) => [x * base.s + base.ox, y * base.s + base.oy] as Pt);

    // With the site panel open on wide screens, the sites move (and shrink if needed) into the space left of it.
    const freeR = W - POPUP_W - 150;
    const alt = wide && freeR - pad.l > 240 ? fit(pts, { x0: pad.l, y0: pad.t, x1: freeR, y1: H - pad.b }) : base;
    const ss = alt.s / base.s;
    const shift = { s: ss, tx: alt.ox - base.ox * ss, ty: alt.oy - base.oy * ss };

    // Region outlines: one padded, smoothed hull per region, built from the plotted sites.
    // NOTE: this outlines where the plotted sites are, not the true limits of the named region.
    const groups = new Map<string, Pt[]>();
    sites.forEach((s, i) => groups.set(regionName(s), [...(groups.get(regionName(s)) ?? []), pos[i]]));
    const R = 64;
    const regions = [...groups].map(([name, ps]) => {
      const ring = ps.flatMap(([x, y]) => Array.from({ length: 24 }, (_, j) => [x + R * Math.cos((j / 24) * 2 * Math.PI), y + R * Math.sin((j / 24) * 2 * Math.PI)] as Pt));
      const hp = hull(ring);
      const top = Math.min(...hp.map((p) => p[1]));
      const cx = ps.reduce((a, p) => a + p[0], 0) / ps.length;
      return { name, d: smoothPath(hp), label: [cx, top - 12] as Pt };
    });
    return { pos, shift, regions };
  }, [sites, W, H, wide]);

  const art = useMemo(() => {
    const r = rng(7);
    const tones = ['#7A3A22', '#5A2A1B', '#8C4A2B', '#3C1D13', '#6B3220', '#A15B36'];
    const blobs = Array.from({ length: 16 }, (_, i) => ({ cx: r(), cy: r(), rx: 0.09 + r() * 0.15, ry: 0.1 + r() * 0.18, fill: tones[i % tones.length], op: 0.22 + r() * 0.3 }));
    const contours = ([[0.43, 0.5, 5], [0.76, 0.3, 3], [0.21, 0.78, 3]] as const).flatMap(([cx, cy, n]) => Array.from({ length: n }, (_, j) => ({ cx, cy, rx: 60 * (j + 1) + 20, ry: 40 * (j + 1) + 15 })));
    const craters = Array.from({ length: 22 }, () => ({ cx: r(), cy: r(), r: 5 + r() * 17 }));
    return { blobs, contours, craters };
  }, []);

  const key = (id: string) => (e: KeyboardEvent) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSelect(id);
    }
  };

  const sh = layout && selectedId ? layout.shift : { s: 1, tx: 0, ty: 0 };
  // Markers, tags, labels and strokes keep their on-screen size at any zoom and while shifted.
  const k = 1 / (zoom * sh.s);

  // De-clutter in screen pixels: nudge overlapping markers apart (a leader line points back to the true spot),
  // then put each tag on the first side where it hits no other tag or marker.
  const placed = useMemo(() => {
    if (!layout) return null;
    const pts = layout.pos.map(([x, y]) => [x / k, y / k] as Pt); // screen px
    const MIN = 42;
    for (let pass = 0; pass < 40; pass++) {
      for (let i = 0; i < pts.length; i++) {
        for (let j = i + 1; j < pts.length; j++) {
          const dx = pts[j][0] - pts[i][0];
          const dy = pts[j][1] - pts[i][1];
          const d = Math.hypot(dx, dy);
          if (d >= MIN) continue;
          const [ux, uy] = d > 0 ? [dx / d, dy / d] : [0, 1];
          const push = (MIN - d) / 2;
          pts[i] = [pts[i][0] - ux * push, pts[i][1] - uy * push];
          pts[j] = [pts[j][0] + ux * push, pts[j][1] + uy * push];
        }
      }
    }
    type Box = [number, number, number, number];
    const hit = (a: Box, b: Box) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
    const taken: Box[] = pts.map(([x, y]) => [x - 18, y - 18, x + 18, y + 18]);
    const order = pts.map((_, i) => i).sort((a, b) => pts[a][1] - pts[b][1]);
    const tags: Pt[] = new Array(pts.length);
    // Screen areas covered by the map's overlays (status pill on top; legend card bottom-left on wide screens).
    const OVERLAYS: Box[] = [[W / 2 - 240, 0, W / 2 + 240, 76], ...(wide ? ([[0, H - 215, 345, H]] as Box[]) : [])];
    const CANDIDATES: Pt[] = [[23, -11], [-101, -11], [23, -36], [23, 14], [-101, -36], [-101, 14], [-39, -50], [-39, 28], [23, -60], [23, 38], [-101, -60], [-101, 38], [-39, -74], [-39, 52]];
    for (const i of order) {
      const [x, y] = pts[i];
      const pick = CANDIDATES.find(([ox, oy]) => {
        const b: Box = [x + ox, y + oy, x + ox + 78, y + oy + 22];
        // Stay on screen: convert to screen px (world shift, then the zoomed view) and keep a 4px margin.
        const ex = (sh.tx - view.x) * zoom;
        const ey = (sh.ty - view.y) * zoom;
        const [sx, sy, sx2, sy2] = [b[0] + ex, b[1] + ey, b[2] + ex, b[3] + ey];
        if (sx < 4 || sx2 > W - 4 || sy < 4 || sy2 > H - 4) return false;
        if (OVERLAYS.some((o) => hit(o, [sx, sy, sx2, sy2]))) return false;
        return !taken.some((t, j) => j !== i && hit(t, b));
      }) ?? CANDIDATES[0];
      tags[i] = pick;
      taken.push([x + pick[0], y + pick[1], x + pick[0] + 78, y + pick[1] + 22]);
    }
    return { pos: pts.map(([x, y]) => [x * k, y * k] as Pt), tags };
  }, [layout, k, zoom, sh.tx, sh.ty, view.x, view.y, W, H, wide]);

  return (
    <svg
      ref={svgRef}
      className={`map mars-map${vb ? ' is-zoomed' : ''}`}
      viewBox={W && H ? `${view.x} ${view.y} ${view.w} ${view.h}` : undefined}
      preserveAspectRatio="xMidYMid meet"
      role="group"
      aria-label="Mars candidate landing sites"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}
      onClickCapture={(e) => {
        if (!suppressClick.current) return;
        suppressClick.current = false;
        e.stopPropagation();
        e.preventDefault();
      }}
      onDoubleClick={() => setView(null)}
    >
      <defs>
        <radialGradient id="marsBase" cx="0.45" cy="0.5" r="0.85">
          <stop offset="0" stopColor="#5B2B1C" />
          <stop offset="0.6" stopColor="#2E1710" />
          <stop offset="1" stopColor="#170C08" />
        </radialGradient>
        <radialGradient id="suit" cx="0.5" cy="0.5" r="0.5">
          <stop offset="0" stopColor="#5BD6A3" stopOpacity="0.34" />
          <stop offset="1" stopColor="#5BD6A3" stopOpacity="0" />
        </radialGradient>
        <filter id="terrainBlur" filterUnits="userSpaceOnUse" x={-W} y={-H} width={W * 3} height={H * 3}>
          <feGaussianBlur stdDeviation="55" />
        </filter>
      </defs>
      {/* Background terrain fills the frame and never moves with the sites, so no edge ever shows. */}
      <rect width={W} height={H} fill="url(#marsBase)" />
      <g filter="url(#terrainBlur)" aria-hidden="true">
        {art.blobs.map((b, i) => (
          <ellipse key={i} cx={b.cx * W} cy={b.cy * H} rx={b.rx * W} ry={b.ry * H} fill={b.fill} opacity={b.op} />
        ))}
      </g>
      <g aria-hidden="true" fill="none">
        {art.contours.map((c, i) => (
          <ellipse key={i} cx={c.cx * W} cy={c.cy * H} rx={c.rx} ry={c.ry} stroke="#ECEFF5" strokeOpacity="0.06" />
        ))}
        {art.craters.map((c, i) => (
          <circle key={i} cx={c.cx * W} cy={c.cy * H} r={c.r} stroke="#ECEFF5" strokeOpacity="0.14" strokeWidth="1.2" />
        ))}
      </g>

      {layout && (
        // Sites, outlines and labels move together into the free area when the site panel opens.
        <g className="mars-world" style={{ transform: `translate(${sh.tx}px, ${sh.ty}px) scale(${sh.s})` } as CSSProperties}>
          <g aria-hidden="true">
            {layout.regions.map((r) => (
              <g key={r.name}>
                <path d={r.d} fill="#72C8EA" fillOpacity="0.07" stroke="#72C8EA" strokeOpacity="0.85" strokeWidth={1.6 * k} strokeDasharray={`${7 * k} ${6 * k}`} />
                <text x={r.label[0]} y={r.label[1]} textAnchor="middle" className="map-region-label" style={{ fontSize: `${12 * k}px` }}>
                  {r.name.toUpperCase()}
                </text>
              </g>
            ))}
          </g>

          {sites.map((s, i) => (
            <ellipse key={s.id + 'g'} cx={layout.pos[i][0]} cy={layout.pos[i][1]} rx={95 * k} ry={72 * k} fill="url(#suit)" pointerEvents="none" />
          ))}

          {placed &&
            sites.map((s, i) => {
              const [tx, ty] = layout.pos[i];
              const [x, y] = placed.pos[i];
              if (Math.hypot(x - tx, y - ty) < 2 * k) return null;
              return (
                <g key={s.id + 'l'} aria-hidden="true" pointerEvents="none">
                  <line className="pin-leader" x1={tx} y1={ty} x2={x} y2={y} strokeWidth={1.2 * k} />
                  <circle className="pin-origin" cx={tx} cy={ty} r={3 * k} />
                </g>
              );
            })}

          {placed && sites.map((s, i) => {
            const sel = s.id === selectedId;
            const score = scoreOf(s);
            const scoreText = score == null ? '—' : String(Math.round(score));
            const [x, y] = placed.pos[i];
            const [ox, oy] = placed.tags[i];
            return (
              <g
                key={s.id}
                className={`site${sel ? ' site-selected' : ''}`}
                transform={`translate(${x} ${y}) scale(${k})`}
                role="button"
                tabIndex={0}
                aria-label={`${s.name}, ${score == null ? 'score unavailable' : `score ${scoreText}`}${s.isSample ? ', sample site' : ''}${s.approxCoords ? ', approximate coordinates' : ''}${sel ? ', selected' : ''}`}
                aria-pressed={sel}
                onClick={() => onSelect(s.id)}
                onKeyDown={key(s.id)}
              >
                <circle r="30" fill="transparent" />
                {sel && <circle className="site-pulse" r="24" fill="none" />}
                <circle className="site-dot" r={sel ? 20 : 15} />
                <text className="site-num" textAnchor="middle" dy="0.35em">
                  {markerLabel(s)}
                </text>
                <g transform={`translate(${ox + (sel ? (ox > 0 ? 5 : -5) : 0)} ${oy})`}>
                  <rect className="site-tag-bg" width="78" height="22" rx="8" />
                  <text className="site-tag" x="39" y="11" textAnchor="middle" dy="0.35em">
                    {s.id} · {scoreText}
                  </text>
                </g>
              </g>
            );
          })}
        </g>
      )}
    </svg>
  );
}
