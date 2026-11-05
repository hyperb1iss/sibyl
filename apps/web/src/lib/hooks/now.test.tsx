import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NOW_TICK_MS, useNow } from './now';

function Clock({ label }: { label: string }) {
  const now = useNow();
  return <span data-testid={label}>{Math.floor(now / 1000)}</span>;
}

describe('useNow', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-05T10:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('moves every tick, on one interval shared by every caller', () => {
    const setIntervalSpy = vi.spyOn(globalThis, 'setInterval');
    const clearIntervalSpy = vi.spyOn(globalThis, 'clearInterval');
    const { unmount } = render(
      <div>
        {Array.from({ length: 50 }, (_, index) => (
          <Clock key={index} label={`clock-${index}`} />
        ))}
      </div>
    );
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
    const start = Number(screen.getByTestId('clock-0').textContent);

    act(() => {
      vi.advanceTimersByTime(NOW_TICK_MS);
    });
    expect(Number(screen.getByTestId('clock-0').textContent)).toBe(start + NOW_TICK_MS / 1000);
    expect(screen.getByTestId('clock-49').textContent).toBe(
      screen.getByTestId('clock-0').textContent
    );
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);

    unmount();
    expect(clearIntervalSpy).toHaveBeenCalledTimes(1);
  });

  it('starts from the real time when a page mounts after a quiet spell', () => {
    const first = render(<Clock label="first" />);
    first.unmount();
    vi.setSystemTime(new Date('2026-11-05T11:00:00Z'));

    render(<Clock label="later" />);
    expect(Number(screen.getByTestId('later').textContent)).toBe(
      Date.parse('2026-11-05T11:00:00Z') / 1000
    );
  });
});
