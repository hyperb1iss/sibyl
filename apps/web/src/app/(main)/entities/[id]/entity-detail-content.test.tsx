import { describe, expect, it } from 'vitest';
import { descriptionRepeatsContent } from './entity-detail-content';

describe('descriptionRepeatsContent', () => {
  it('is true when both fields carry the same text', () => {
    expect(descriptionRepeatsContent('same body', 'same body ')).toBe(true);
  });

  it('is true when the description is a truncated preview of the content', () => {
    const content = 'Prod alert 2026-09-03 is the same class as 2026-06-17. Kong threshold 8.2KB.';
    expect(descriptionRepeatsContent('Prod alert 2026-09-03 is the same class', content)).toBe(
      true
    );
    expect(descriptionRepeatsContent('Prod alert 2026-09-03 is the same class...', content)).toBe(
      true
    );
    expect(descriptionRepeatsContent('Prod alert 2026-09-03 is the same class…', content)).toBe(
      true
    );
  });

  it('is false when the description says something the content does not', () => {
    expect(descriptionRepeatsContent('A short summary', 'Full body with different words')).toBe(
      false
    );
  });

  it('is false when either side is empty', () => {
    expect(descriptionRepeatsContent('', 'body')).toBe(false);
    expect(descriptionRepeatsContent('desc', '')).toBe(false);
    expect(descriptionRepeatsContent(null, undefined)).toBe(false);
  });
});
