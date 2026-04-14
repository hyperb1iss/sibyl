import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const replace = vi.fn();
const navigationState = vi.hoisted(() => ({
  searchParams: new URLSearchParams(),
}));

const hooks = vi.hoisted(() => ({
  useRawCaptures: vi.fn(),
  useRawCapture: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
  usePathname: () => '/archive',
  useSearchParams: () => navigationState.searchParams,
}));

vi.mock('@/lib/hooks', () => hooks);

import ArchivePage from './page';

const captureList = {
  captures: [
    {
      id: 'raw-1',
      entity_id: 'episode_123',
      title: 'Quick memory',
      entity_type: 'episode',
      tags: ['alpha'],
      metadata: { capture_mode: 'quick', capture_surface: 'dashboard' },
      capture_surface: 'dashboard',
      created_by_user_id: 'user-1',
      created_at: '2026-04-14T16:00:00Z',
    },
    {
      id: 'raw-2',
      entity_id: null,
      title: 'Deep thought',
      entity_type: 'pattern',
      tags: ['beta'],
      metadata: { capture_mode: 'quick', capture_surface: 'cli' },
      capture_surface: 'cli',
      created_by_user_id: null,
      created_at: '2026-04-14T15:30:00Z',
    },
  ],
  limit: 200,
  offset: 0,
  has_more: false,
};

describe('ArchivePage', () => {
  beforeEach(() => {
    replace.mockReset();
    navigationState.searchParams = new URLSearchParams();
    hooks.useRawCaptures.mockReturnValue({
      data: captureList,
      isLoading: false,
      error: null,
    });
    hooks.useRawCapture.mockImplementation((id: string) => ({
      data:
        id === 'raw-2'
          ? {
              ...captureList.captures[1],
              raw_content: 'remember this exact text from the terminal',
            }
          : {
              ...captureList.captures[0],
              raw_content: 'remember this exact text from the dashboard',
            },
      isLoading: false,
      error: null,
    }));
  });

  it('renders the archive detail pane with verbatim content', () => {
    render(<ArchivePage />);

    expect(screen.getByText('Verbatim Content')).toBeInTheDocument();
    expect(screen.getByText('remember this exact text from the dashboard')).toBeInTheDocument();
    expect(screen.getAllByText('Quick memory').length).toBeGreaterThan(0);
  });

  it('updates the route when selecting a different capture', async () => {
    const { user } = render(<ArchivePage />);

    await user.click(screen.getByRole('button', { name: /select capture deep thought/i }));

    expect(replace).toHaveBeenCalledWith('/archive?id=raw-2', { scroll: false });
  });

  it('replaces a filtered-out selection with the visible capture id', async () => {
    navigationState.searchParams = new URLSearchParams('id=raw-2');

    const { user } = render(<ArchivePage />);

    await user.type(screen.getByPlaceholderText(/search titles, tags, metadata/i), 'alpha');

    expect(replace).toHaveBeenCalledWith('/archive?id=raw-1', { scroll: false });
  });
});
