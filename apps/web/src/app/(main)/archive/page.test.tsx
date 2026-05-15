import { describe, expect, it, vi } from 'vitest';

const redirect = vi.hoisted(() =>
  vi.fn((href: string) => {
    throw new Error(`NEXT_REDIRECT:${href}`);
  })
);

vi.mock('next/navigation', () => ({ redirect }));

import ArchivePage from './page';

describe('ArchivePage', () => {
  it('redirects legacy archive traffic into memory captures', async () => {
    await expect(ArchivePage({ searchParams: Promise.resolve({}) })).rejects.toThrow(
      'NEXT_REDIRECT:/memory/captures'
    );

    expect(redirect).toHaveBeenCalledWith('/memory/captures');
  });

  it('preserves query parameters when redirecting', async () => {
    await expect(
      ArchivePage({
        searchParams: Promise.resolve({
          id: 'raw-2',
          link: 'unlinked',
          tag: ['alpha', 'beta'],
          empty: undefined,
        }),
      })
    ).rejects.toThrow('NEXT_REDIRECT:/memory/captures?id=raw-2&link=unlinked&tag=alpha&tag=beta');

    expect(redirect).toHaveBeenCalledWith(
      '/memory/captures?id=raw-2&link=unlinked&tag=alpha&tag=beta'
    );
  });
});
