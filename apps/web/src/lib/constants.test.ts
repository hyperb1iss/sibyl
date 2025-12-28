import { describe, expect, it } from 'vitest';
import {
  ENTITY_COLORS,
  formatDateTime,
  formatUptime,
  TASK_PRIORITIES,
  TASK_PRIORITY_CONFIG,
  TASK_STATUS_CONFIG,
  TASK_STATUSES,
} from './constants';

describe('TASK_STATUSES', () => {
  it('contains all expected statuses', () => {
    expect(TASK_STATUSES).toContain('backlog');
    expect(TASK_STATUSES).toContain('todo');
    expect(TASK_STATUSES).toContain('doing');
    expect(TASK_STATUSES).toContain('blocked');
    expect(TASK_STATUSES).toContain('review');
    expect(TASK_STATUSES).toContain('done');
  });

  it('has config for every status', () => {
    for (const status of TASK_STATUSES) {
      expect(TASK_STATUS_CONFIG[status]).toBeDefined();
      expect(TASK_STATUS_CONFIG[status].label).toBeDefined();
      expect(TASK_STATUS_CONFIG[status].bgClass).toBeDefined();
      expect(TASK_STATUS_CONFIG[status].textClass).toBeDefined();
    }
  });
});

describe('TASK_PRIORITIES', () => {
  it('contains all expected priorities in order', () => {
    expect(TASK_PRIORITIES).toEqual(['critical', 'high', 'medium', 'low', 'someday']);
  });

  it('has config for every priority', () => {
    for (const priority of TASK_PRIORITIES) {
      expect(TASK_PRIORITY_CONFIG[priority]).toBeDefined();
      expect(TASK_PRIORITY_CONFIG[priority].label).toBeDefined();
    }
  });
});

describe('ENTITY_COLORS', () => {
  it('has color for common entity types', () => {
    expect(ENTITY_COLORS.pattern).toBeDefined();
    expect(ENTITY_COLORS.task).toBeDefined();
    expect(ENTITY_COLORS.project).toBeDefined();
    expect(ENTITY_COLORS.episode).toBeDefined();
  });

  it('returns valid hex colors', () => {
    const hexColorRegex = /^#[0-9A-Fa-f]{6}$/;
    for (const color of Object.values(ENTITY_COLORS)) {
      expect(color).toMatch(hexColorRegex);
    }
  });
});

describe('formatDateTime', () => {
  it('formats ISO date string', () => {
    const result = formatDateTime('2024-03-15T10:30:00Z');
    expect(result).toContain('2024');
    expect(result).toContain('Mar');
    expect(result).toContain('15');
  });

  it('formats Date object', () => {
    const date = new Date('2024-06-20T14:00:00Z');
    const result = formatDateTime(date);
    expect(result).toContain('2024');
    expect(result).toContain('Jun');
  });
});

describe('formatUptime', () => {
  it('formats seconds only when under a minute', () => {
    expect(formatUptime(0)).toBe('0s');
    expect(formatUptime(45)).toBe('45s');
    expect(formatUptime(59)).toBe('59s');
  });

  it('formats minutes only when under an hour', () => {
    expect(formatUptime(60)).toBe('1m');
    expect(formatUptime(90)).toBe('1m'); // Rounds down
    expect(formatUptime(3599)).toBe('59m');
  });

  it('formats hours only when under a day', () => {
    expect(formatUptime(3600)).toBe('1h');
    expect(formatUptime(7200)).toBe('2h');
    expect(formatUptime(86399)).toBe('23h');
  });

  it('formats days for 24+ hours', () => {
    expect(formatUptime(86400)).toBe('1d');
    expect(formatUptime(172800)).toBe('2d');
  });
});
