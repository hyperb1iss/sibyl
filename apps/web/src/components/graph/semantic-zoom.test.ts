import { describe, expect, it } from 'vitest';
import {
  type ClusterExtent,
  clusterRadius,
  memberSeedPosition,
  resolveExpandedClusters,
  SEMANTIC_ZOOM,
  type Viewport,
  zoomLevelName,
} from './semantic-zoom';

const VIEWPORT: Viewport = { minX: -500, minY: -500, maxX: 500, maxY: 500 };

function cluster(id: string, memberCount: number, x = 0, y = 0): ClusterExtent {
  return { id, memberCount, x, y };
}

/** Zoom at which a cluster of this size crosses a given screen radius. */
function zoomFor(memberCount: number, screenRadius: number): number {
  return screenRadius / clusterRadius(memberCount);
}

describe('resolveExpandedClusters', () => {
  it('keeps every cluster collapsed when zoomed out', () => {
    const clusters = [cluster('a', 900), cluster('b', 40), cluster('c', 12)];

    const expanded = resolveExpandedClusters({
      clusters,
      zoom: 0.5,
      viewport: VIEWPORT,
      previous: new Set(),
    });

    expect(expanded.size).toBe(0);
  });

  it('opens a large domain before a small one at the same zoom', () => {
    const big = cluster('big', 900);
    const small = cluster('small', 8);
    const zoom = zoomFor(900, SEMANTIC_ZOOM.EXPAND_SCREEN_RADIUS + 1);

    const expanded = resolveExpandedClusters({
      clusters: [big, small],
      zoom,
      viewport: VIEWPORT,
      previous: new Set(),
    });

    expect([...expanded]).toEqual(['big']);
  });

  it('holds an open cluster through the hysteresis band', () => {
    const clusters = [cluster('a', 200)];
    const betweenThresholds = zoomFor(
      200,
      (SEMANTIC_ZOOM.EXPAND_SCREEN_RADIUS + SEMANTIC_ZOOM.COLLAPSE_SCREEN_RADIUS) / 2
    );

    const fromCollapsed = resolveExpandedClusters({
      clusters,
      zoom: betweenThresholds,
      viewport: VIEWPORT,
      previous: new Set(),
    });
    const fromExpanded = resolveExpandedClusters({
      clusters,
      zoom: betweenThresholds,
      viewport: VIEWPORT,
      previous: new Set(['a']),
    });

    expect(fromCollapsed.has('a')).toBe(false);
    expect(fromExpanded.has('a')).toBe(true);
  });

  it('collapses once the bubble shrinks below the lower threshold', () => {
    const expanded = resolveExpandedClusters({
      clusters: [cluster('a', 200)],
      zoom: zoomFor(200, SEMANTIC_ZOOM.COLLAPSE_SCREEN_RADIUS - 5),
      viewport: VIEWPORT,
      previous: new Set(['a']),
    });

    expect(expanded.has('a')).toBe(false);
  });

  it('leaves clusters far outside the viewport collapsed however far in the camera is', () => {
    const near = cluster('near', 300, 0, 0);
    const far = cluster('far', 300, 20_000, 20_000);

    const expanded = resolveExpandedClusters({
      clusters: [near, far],
      zoom: 8,
      viewport: VIEWPORT,
      previous: new Set(),
    });

    expect(expanded.has('near')).toBe(true);
    expect(expanded.has('far')).toBe(false);
  });

  it('expands a cluster the reader pinned regardless of size or position', () => {
    const expanded = resolveExpandedClusters({
      clusters: [cluster('tiny', 3, 90_000, 90_000)],
      zoom: 0.2,
      viewport: VIEWPORT,
      previous: new Set(),
      pinned: new Set(['tiny']),
    });

    expect(expanded.has('tiny')).toBe(true);
  });

  it('keeps a pin even when the cluster list no longer holds it', () => {
    const expanded = resolveExpandedClusters({
      clusters: [],
      zoom: 0.1,
      viewport: VIEWPORT,
      previous: new Set(),
      pinned: new Set(['gone']),
    });

    expect(expanded.has('gone')).toBe(true);
  });

  it('treats an unplaced cluster as on screen so the first frame can expand', () => {
    const unplaced: ClusterExtent = { id: 'a', memberCount: 400 };

    const expanded = resolveExpandedClusters({
      clusters: [unplaced],
      zoom: zoomFor(400, SEMANTIC_ZOOM.EXPAND_SCREEN_RADIUS + 1),
      viewport: VIEWPORT,
      previous: new Set(),
    });

    expect(expanded.has('a')).toBe(true);
  });
});

describe('zoomLevelName', () => {
  it('names the three levels', () => {
    expect(zoomLevelName(10, 0)).toBe('domains');
    expect(zoomLevelName(10, 4)).toBe('mixed');
    expect(zoomLevelName(10, 10)).toBe('entities');
  });

  it('does not call an empty graph fully expanded', () => {
    expect(zoomLevelName(0, 0)).toBe('domains');
  });
});

describe('memberSeedPosition', () => {
  it('rings members around the bubble they came from', () => {
    const first = memberSeedPosition(100, -50, 0, 4, 64);
    const second = memberSeedPosition(100, -50, 2, 4, 64);

    expect(first.x).toBeGreaterThan(100);
    expect(second.x).toBeLessThan(100);
    expect(Math.hypot(first.x - 100, first.y + 50)).toBeCloseTo(
      Math.hypot(second.x - 100, second.y + 50),
      5
    );
  });

  it('is stable for the same member across reopens', () => {
    expect(memberSeedPosition(0, 0, 3, 9, 100)).toEqual(memberSeedPosition(0, 0, 3, 9, 100));
  });
});
