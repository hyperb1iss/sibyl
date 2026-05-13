import { describe, expect, it } from 'vitest';
import { isSetupAlreadyInitializedError } from './api';

describe('isSetupAlreadyInitializedError', () => {
  it('matches structured setup initialization errors', () => {
    const error = new Error(
      '{"detail":{"code":"setup_already_initialized","message":"Setup is complete."}}'
    );

    expect(isSetupAlreadyInitializedError(error)).toBe(true);
  });

  it('matches legacy setup complete messages', () => {
    expect(isSetupAlreadyInitializedError(new Error('Setup is complete.'))).toBe(true);
  });

  it('ignores unrelated errors and non-errors', () => {
    expect(isSetupAlreadyInitializedError(new Error('Admin or owner role required'))).toBe(false);
    expect(isSetupAlreadyInitializedError('setup_already_initialized')).toBe(false);
  });
});
