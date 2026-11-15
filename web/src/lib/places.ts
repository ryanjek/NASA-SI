import { geoContains, geoDistance } from 'd3-geo';
import { feature } from 'topojson-client';
import type { Feature, Geometry } from 'geojson';

/**
 * Country names for Earth cells, from the world-atlas 1:110m borders (already a dependency).
 * Inside a border -> that country. Within ~150 km of one (coasts, small islands the 1:110m map drops) -> "near X".
 * Otherwise null, and the UI shows the coordinates instead. Loaded on demand.
 */
type Country = { name: string; f: Feature<Geometry, { name: string }>; pts: [number, number][] };
let countries: Promise<Country[]> | null = null;

function load(): Promise<Country[]> {
  countries ??= import('world-atlas/countries-110m.json').then((m) => {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const topo = m.default as any;
    const fc = feature(topo, topo.objects.countries) as unknown as { features: Feature<Geometry, { name: string }>[] };
    return fc.features.map((f) => ({ name: f.properties.name, f, pts: vertices(f.geometry) }));
  });
  return countries;
}

function vertices(g: Geometry | null): [number, number][] {
  if (!g) return [];
  if (g.type === 'Polygon') return g.coordinates.flat() as [number, number][];
  if (g.type === 'MultiPolygon') return g.coordinates.flat(2) as [number, number][];
  return [];
}

const NEAR_RAD = 150 / 6371; // 150 km as an angle

export interface Place {
  country: string;
  near: boolean;
}

function lookup(list: Country[], lon: number, lat: number): Place | null {
  const p: [number, number] = [lon, lat];
  for (const c of list) if (geoContains(c.f, p)) return { country: c.name, near: false };
  let best: Place | null = null;
  let bestD = NEAR_RAD;
  for (const c of list)
    for (const v of c.pts) {
      const d = geoDistance(p, v);
      if (d < bestD) {
        bestD = d;
        best = { country: c.name, near: true };
      }
    }
  return best;
}

/** Places for many points at once, keyed by `key`. */
export async function placesFor<T>(items: readonly T[], key: (t: T) => string, lonLat: (t: T) => [number, number]): Promise<Map<string, Place | null>> {
  const list = await load();
  const out = new Map<string, Place | null>();
  for (const it of items) {
    const [lon, lat] = lonLat(it);
    out.set(key(it), lookup(list, lon, lat));
  }
  return out;
}

export const placeText = (p: Place | null | undefined) => (p ? (p.near ? `Near ${p.country}` : p.country) : null);
