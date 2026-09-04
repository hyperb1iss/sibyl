import { describe, expect, it } from 'vitest';
import { formatScorePercent } from './formatting';

describe('formatScorePercent', () => {
  it('rounds a unit-interval score to a whole percent', () => {
    expect(formatScorePercent(0.734)).toBe(73);
    expect(formatScorePercent(1)).toBe(100);
    expect(formatScorePercent(0)).toBe(0);
  });

  it('clamps fused scores that land above 1.0', () => {
    expect(formatScorePercent(1.66)).toBe(100);
    expect(formatScorePercent(42)).toBe(100);
  });

  it('never goes negative or NaN', () => {
    expect(formatScorePercent(-0.2)).toBe(0);
    expect(formatScorePercent(Number.NaN)).toBe(0);
    expect(formatScorePercent(undefined)).toBe(0);
    expect(formatScorePercent(null)).toBe(0);
  });
});
