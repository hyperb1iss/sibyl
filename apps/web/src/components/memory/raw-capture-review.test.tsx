import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';

const replace = vi.fn();
const navigationState = vi.hoisted(() => ({
  searchParams: new URLSearchParams(),
}));
const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

const hooks = vi.hoisted(() => ({
  useRawCaptures: vi.fn(),
  useRawCapture: vi.fn(),
  useUpdateRawCaptureReviewState: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace }),
  usePathname: () => '/memory/captures',
  useSearchParams: () => navigationState.searchParams,
}));

vi.mock('@/lib/hooks', () => hooks);
vi.mock('sonner', () => ({ toast }));

import { RawCaptureReview } from './raw-capture-review';

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
      review_state: 'pending',
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
      review_state: 'pending',
      created_by_user_id: null,
      created_at: '2026-04-14T15:30:00Z',
    },
  ],
  limit: 200,
  offset: 0,
  has_more: false,
};

describe('RawCaptureReview', () => {
  beforeEach(() => {
    replace.mockReset();
    toast.success.mockReset();
    toast.error.mockReset();
    navigationState.searchParams = new URLSearchParams();
    hooks.useRawCaptures.mockReturnValue({
      data: captureList,
      isLoading: false,
      error: null,
    });
    hooks.useUpdateRawCaptureReviewState.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
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

  it('renders the memory capture detail pane with verbatim content', () => {
    render(<RawCaptureReview />);

    expect(screen.getByText('Memory Captures')).toBeInTheDocument();
    expect(screen.getByText('Verbatim Content')).toBeInTheDocument();
    expect(screen.getByText('remember this exact text from the dashboard')).toBeInTheDocument();
    expect(screen.getAllByText('Quick memory').length).toBeGreaterThan(0);
    expect(screen.getByText('Needs link')).toBeInTheDocument();
  });

  it('updates the memory captures route when selecting a different capture', async () => {
    const { user } = render(<RawCaptureReview />);

    await user.click(screen.getByRole('button', { name: /select capture deep thought/i }));

    expect(replace).toHaveBeenCalledWith('/memory/captures?id=raw-2', { scroll: false });
  });

  it('replaces a filtered-out selection with the visible capture id', async () => {
    navigationState.searchParams = new URLSearchParams('id=raw-2');

    const { user } = render(<RawCaptureReview />);

    await user.type(screen.getByPlaceholderText(/search titles, tags, metadata/i), 'alpha');

    expect(replace).toHaveBeenCalledWith('/memory/captures?id=raw-1', { scroll: false });
  });

  it('filters captures down to entries that still need linking', async () => {
    const { user } = render(<RawCaptureReview />);

    await user.click(screen.getByRole('button', { name: /needs link1/i }));

    expect(
      screen.getByRole('button', { name: /select capture deep thought/i })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /select capture quick memory/i })
    ).not.toBeInTheDocument();
    expect(screen.getByText('remember this exact text from the terminal')).toBeInTheDocument();
  });

  it('starts in the needs-link queue when requested in the url', () => {
    navigationState.searchParams = new URLSearchParams('link=unlinked');

    render(<RawCaptureReview />);

    expect(screen.getByText('Needs Link Queue')).toBeInTheDocument();
    expect(screen.getByText('remember this exact text from the terminal')).toBeInTheDocument();
    expect(
      screen.queryByText('remember this exact text from the dashboard')
    ).not.toBeInTheDocument();
  });

  it('advances review navigation from the detail pane', async () => {
    const { user } = render(<RawCaptureReview />);

    await user.click(screen.getByRole('button', { name: 'Next' }));

    expect(replace).toHaveBeenCalledWith('/memory/captures?id=raw-2', { scroll: false });
  });

  it('sends a defer action for the selected capture', async () => {
    const mutateAsync = vi.fn().mockResolvedValue({
      ...captureList.captures[0],
      raw_content: 'remember this exact text from the dashboard',
      review_state: 'deferred',
    });
    hooks.useUpdateRawCaptureReviewState.mockReturnValue({
      mutateAsync,
      isPending: false,
    });

    const { user } = render(<RawCaptureReview />);

    await user.click(screen.getByRole('button', { name: 'Defer' }));

    expect(mutateAsync).toHaveBeenCalledWith({ id: 'raw-1', reviewState: 'deferred' });
  });
});
