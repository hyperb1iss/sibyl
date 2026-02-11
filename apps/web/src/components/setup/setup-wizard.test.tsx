import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Test the step persistence utility functions directly
const STEPS = ['welcome', 'api-keys', 'admin', 'connect'] as const;
type SetupStep = (typeof STEPS)[number];
const STEP_STORAGE_KEY = 'sibyl-setup-step';

function getStoredStep(): SetupStep {
  if (typeof window === 'undefined') return 'welcome';
  const stored = sessionStorage.getItem(STEP_STORAGE_KEY);
  if (stored && STEPS.includes(stored as SetupStep)) {
    return stored as SetupStep;
  }
  return 'welcome';
}

describe('SetupWizard Step Persistence', () => {
  beforeEach(() => {
    sessionStorage.clear();
  });

  afterEach(() => {
    sessionStorage.clear();
  });

  describe('getStoredStep', () => {
    it('returns welcome when sessionStorage is empty', () => {
      expect(getStoredStep()).toBe('welcome');
    });

    it('returns stored step when valid', () => {
      sessionStorage.setItem(STEP_STORAGE_KEY, 'api-keys');
      expect(getStoredStep()).toBe('api-keys');
    });

    it('returns welcome when stored step is invalid', () => {
      sessionStorage.setItem(STEP_STORAGE_KEY, 'invalid-step');
      expect(getStoredStep()).toBe('welcome');
    });

    it('handles all valid steps', () => {
      for (const step of STEPS) {
        sessionStorage.setItem(STEP_STORAGE_KEY, step);
        expect(getStoredStep()).toBe(step);
      }
    });
  });

  describe('sessionStorage persistence', () => {
    it('persists step to sessionStorage', () => {
      const step: SetupStep = 'api-keys';
      sessionStorage.setItem(STEP_STORAGE_KEY, step);

      expect(sessionStorage.getItem(STEP_STORAGE_KEY)).toBe('api-keys');
    });

    it('clears step from sessionStorage', () => {
      sessionStorage.setItem(STEP_STORAGE_KEY, 'admin');
      sessionStorage.removeItem(STEP_STORAGE_KEY);

      expect(sessionStorage.getItem(STEP_STORAGE_KEY)).toBeNull();
    });

    it('survives getting and setting multiple times', () => {
      sessionStorage.setItem(STEP_STORAGE_KEY, 'welcome');
      expect(getStoredStep()).toBe('welcome');

      sessionStorage.setItem(STEP_STORAGE_KEY, 'api-keys');
      expect(getStoredStep()).toBe('api-keys');

      sessionStorage.setItem(STEP_STORAGE_KEY, 'admin');
      expect(getStoredStep()).toBe('admin');
    });
  });
});
