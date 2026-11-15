import { describe, expect, test } from 'bun:test';
import { fixtureAnalogs, fixtureMars } from './fixtures';
import { plural } from './format';
import { ANALOGS } from '../data/analogs';
import { MARS_SITES } from '../data/marsSites';

describe('plural', () => {
  test('1 is singular, everything else plural', () => {
    expect(plural(1, 'analog site', 'analog sites')).toBe('1 analog site');
    expect(plural(0, 'analog site', 'analog sites')).toBe('0 analog sites');
    expect(plural(5, 'analog site', 'analog sites')).toBe('5 analog sites');
  });
});

describe('stress fixtures (break-ui)', () => {
  test('demo leaves data unchanged', () => {
    expect(fixtureAnalogs(ANALOGS, 'demo')).toEqual([...ANALOGS]);
    expect(fixtureMars(MARS_SITES, 'demo')).toEqual([...MARS_SITES]);
  });

  test('worst: long, realistic names and regions appear', () => {
    const w = fixtureAnalogs(ANALOGS, 'worst');
    expect(Math.max(...w.map((a) => a.name.length))).toBeGreaterThan(40);
    expect(Math.max(...w.map((a) => a.region.length))).toBeGreaterThan(45);
    const sites = fixtureMars(MARS_SITES, 'worst');
    expect(sites.some((s) => s.description.length > 500)).toBe(true);
  });

  test('one: exactly one analog per continent', () => {
    const o = fixtureAnalogs(ANALOGS, 'one');
    const counts = new Map<string, number>();
    for (const a of o) counts.set(a.continent, (counts.get(a.continent) ?? 0) + 1);
    expect([...counts.values()].every((n) => n === 1)).toBe(true);
  });

  test('empty: Oceania and Europe have no analogs', () => {
    const e = fixtureAnalogs(ANALOGS, 'empty');
    expect(e.some((a) => a.continent === 'OC' || a.continent === 'EU')).toBe(false);
  });

  test('many: Asia grows large with unique ids', () => {
    const m = fixtureAnalogs(ANALOGS, 'many');
    expect(m.filter((a) => a.continent === 'AS').length).toBeGreaterThan(40);
    expect(new Set(m.map((a) => a.id)).size).toBe(m.length);
  });

  test('nosites: no Mars candidates', () => {
    expect(fixtureMars(MARS_SITES, 'nosites')).toEqual([]);
  });
});
