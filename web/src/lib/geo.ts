import { geoGraticule10, geoNaturalEarth1, geoPath } from 'd3-geo';
import { feature } from 'topojson-client';
import landTopo from 'world-atlas/land-110m.json';
import type { ContinentId } from '../data/analogs';

/** Base map space. The SVG viewBox is `VB`; the world sits inside it at k=0.88. */
export const W = 1000;
export const H = 520;
export const VB = { w: 1000, h: 804 };

const projection = geoNaturalEarth1().fitExtent(
  [
    [6, 6],
    [W - 6, H - 6],
  ],
  { type: 'Sphere' },
);
const pathGen = geoPath(projection);

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const topo = landTopo as any;
export const landPath: string = pathGen(feature(topo, topo.objects.land) as never) ?? '';
export const graticulePath: string = pathGen(geoGraticule10()) ?? '';
export const spherePath: string = pathGen({ type: 'Sphere' }) ?? '';

export const project = (lon: number, lat: number): [number, number] => (projection([lon, lat]) ?? [0, 0]) as [number, number];

/** Rough continent outlines (lon, lat) used as click targets and highlight masks. */
const RINGS: Record<ContinentId, [number, number][]> = {
  NA: [[-168,66],[-156,71],[-125,72],[-124,77],[-100,81],[-75,83],[-30,84],[-18,78],[-22,70],[-44,59],[-62,58],[-56,50],[-67,44],[-76,36],[-81,26],[-90,29],[-97,26],[-97,19],[-88,16],[-83,9],[-79,8],[-86,13],[-95,16],[-105,20],[-112,29],[-118,33],[-124,40],[-124,48],[-135,58],[-150,60],[-165,58]],
  SA: [[-80,9],[-72,12],[-62,10],[-50,0],[-35,-6],[-40,-22],[-48,-28],[-58,-38],[-65,-42],[-68,-52],[-72,-52],[-74,-40],[-71,-20],[-76,-14],[-81,-5],[-78,2]],
  EU: [[-10,36],[-9,43],[-2,44],[-5,48],[3,51],[8,54],[10,57],[5,62],[15,69],[28,71],[45,68],[60,68],[60,45],[47,42],[40,41],[28,41],[26,40],[22,37],[19,41],[14,38],[12,44],[3,43],[-1,37]],
  AS: [[60,68],[70,73],[100,77],[130,72],[160,70],[180,68],[170,60],[160,55],[142,52],[140,44],[130,42],[122,38],[121,31],[122,24],[108,21],[106,10],[100,13],[100,2],[104,1],[98,8],[92,22],[80,15],[77,8],[72,20],[66,25],[57,25],[56,27],[50,30],[48,29],[56,22],[59,22],[52,16],[43,13],[39,21],[34,28],[35,32],[27,37],[28,41],[40,41],[47,42],[60,45]],
  AF: [[-17,21],[-10,30],[-6,36],[10,37],[11,33],[20,31],[32,31],[35,28],[43,12],[51,12],[40,-3],[40,-15],[35,-25],[27,-34],[18,-34],[12,-17],[13,-5],[9,4],[-8,4],[-17,14]],
  OC: [[112,-10],[130,-9],[142,-9],[154,-26],[153,-38],[141,-39],[131,-32],[114,-35],[113,-21]],
};

export interface ContinentShape {
  id: ContinentId;
  name: string;
  path: string;
  bbox: { cx: number; cy: number; w: number; h: number };
  labelAt: [number, number];
}

const CONTINENT_NAMES: Record<ContinentId, string> = { NA: 'North America', SA: 'South America', EU: 'Europe', AF: 'Africa', AS: 'Asia', OC: 'Oceania' };
const LABEL_LONLAT: Record<ContinentId, [number, number]> = { NA: [-104, 47], SA: [-52, -16], EU: [14, 54], AF: [20, 6], AS: [104, 56], OC: [134, -26] };

export const CONTINENTS: readonly ContinentShape[] = (Object.keys(RINGS) as ContinentId[]).map((id) => {
  const pts = RINGS[id].map(([lo, la]) => project(lo, la));
  const xs = pts.map((p) => p[0]);
  const ys = pts.map((p) => p[1]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  return {
    id,
    name: CONTINENT_NAMES[id],
    path: 'M' + pts.map((p) => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join('L') + 'Z',
    bbox: { cx: (x0 + x1) / 2, cy: (y0 + y1) / 2, w: x1 - x0, h: y1 - y0 },
    labelAt: project(...LABEL_LONLAT[id]),
  };
});

export interface View {
  k: number;
  tx: number;
  ty: number;
}

/** World at rest: smaller than the viewBox so overlays never cover it. */
export const WORLD_VIEW: View = { k: 0.88, tx: 60, ty: 280 };

/** Transform that frames a continent inside the free map area. */
export function continentView(id: ContinentId): View {
  const c = CONTINENTS.find((x) => x.id === id);
  if (!c) return WORLD_VIEW;
  const k = Math.min(3.4, Math.max(0.88, Math.min(640 / c.bbox.w, 470 / c.bbox.h)));
  return { k, tx: 540 - k * c.bbox.cx, ty: 440 - k * c.bbox.cy };
}

/** Point-in-polygon on the rough lon/lat rings. */
function inRing(lon: number, lat: number, ring: [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > lat !== yj > lat && lon < ((xj - xi) * (lat - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Continent for a point, from the rough outlines; `null` for islands and coasts outside them. */
export function continentOf(lon: number, lat: number): ContinentId | null {
  const x = lon > 180 ? lon - 360 : lon;
  for (const id of Object.keys(RINGS) as ContinentId[]) if (inRing(x, lat, RINGS[id])) return id;
  return null;
}
