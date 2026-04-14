import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';
import { CaptureMemoryDialog } from './capture-memory-dialog';

const hooks = vi.hoisted(() => ({
  useCreateEntity: vi.fn(),
}));

const toast = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock('@/lib/hooks', () => hooks);
vi.mock('sonner', () => ({ toast }));

describe('CaptureMemoryDialog', () => {
  it('derives a title when only memory content is provided', async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ id: 'entity_123' });
    hooks.useCreateEntity.mockReturnValue({
      mutateAsync,
      isPending: false,
    });

    const onClose = vi.fn();
    const { user } = render(<CaptureMemoryDialog isOpen onClose={onClose} />);

    await user.type(
      screen.getByLabelText('Memory'),
      'Shipped the benchmark entry points and verified the live stack path.'
    );
    await user.click(screen.getByRole('button', { name: 'Capture Memory' }));

    expect(mutateAsync).toHaveBeenCalledWith({
      name: 'Shipped the benchmark entry points and verified the live stack path.',
      content: 'Shipped the benchmark entry points and verified the live stack path.',
      entity_type: 'episode',
      tags: undefined,
      metadata: { capture_mode: 'quick', capture_surface: 'dashboard' },
    });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('uses the typed title and parsed tags when provided', async () => {
    const mutateAsync = vi.fn().mockResolvedValue({ id: 'entity_456' });
    hooks.useCreateEntity.mockReturnValue({
      mutateAsync,
      isPending: false,
    });

    const { user } = render(<CaptureMemoryDialog isOpen onClose={vi.fn()} />);

    await user.click(screen.getByRole('button', { name: /Pattern/ }));
    await user.type(screen.getByLabelText('Title'), 'Reliable ingest pattern');
    await user.type(screen.getByLabelText('Memory'), 'Queue first, then link graph.');
    await user.type(screen.getByLabelText('Tags'), 'ingest, queue , graph');
    await user.click(screen.getByRole('button', { name: 'Capture Memory' }));

    expect(mutateAsync).toHaveBeenCalledWith({
      name: 'Reliable ingest pattern',
      content: 'Queue first, then link graph.',
      entity_type: 'pattern',
      tags: ['ingest', 'queue', 'graph'],
      metadata: { capture_mode: 'quick', capture_surface: 'dashboard' },
    });
  });
});
