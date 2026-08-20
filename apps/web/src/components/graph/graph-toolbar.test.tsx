import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@/test/utils';
import { GraphToolbar } from './graph-toolbar';

function toolbarProps() {
  return {
    resolution: 'detail' as const,
    onResolutionChange: vi.fn(),
    onZoomIn: vi.fn(),
    onZoomOut: vi.fn(),
    onFitView: vi.fn(),
    onReset: vi.fn(),
    isFullscreen: false,
    onToggleFullscreen: vi.fn(),
    searchTerm: '',
    onSearchChange: vi.fn(),
    selectedTypes: [] as string[],
    onTypesChange: vi.fn(),
    matchCount: 0,
    nodeCount: 42,
    edgeCount: 64,
  };
}

describe('GraphToolbar', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('forwards resolution, search, and view controls', async () => {
    const props = toolbarProps();
    const { user } = render(<GraphToolbar {...props} />);

    await user.click(screen.getByRole('button', { name: 'Overview' }));
    await user.type(screen.getByPlaceholderText('Search nodes...'), 'sibyl');
    await user.click(screen.getByTitle('Zoom in'));
    await user.click(screen.getByTitle('Reset view'));

    expect(props.onResolutionChange).toHaveBeenCalledWith('overview');
    expect(props.onSearchChange).toHaveBeenCalledTimes(5);
    expect(props.onZoomIn).toHaveBeenCalledOnce();
    expect(props.onReset).toHaveBeenCalledOnce();
  });

  it('adds and clears entity type filters without changing their order', async () => {
    const props = toolbarProps();
    const view = render(<GraphToolbar {...props} />);

    await view.user.click(screen.getByRole('button', { name: 'Types' }));
    await view.user.click(screen.getByRole('button', { name: 'Tasks' }));
    expect(props.onTypesChange).toHaveBeenLastCalledWith(['task']);

    view.rerender(<GraphToolbar {...props} selectedTypes={['task']} />);
    await view.user.click(screen.getByRole('button', { name: 'Clear filter' }));
    expect(props.onTypesChange).toHaveBeenLastCalledWith([]);
  });

  it('closes the type menu on an outside pointer action', async () => {
    const props = toolbarProps();
    const { user } = render(<GraphToolbar {...props} />);

    await user.click(screen.getByRole('button', { name: 'Types' }));
    expect(screen.getByRole('button', { name: 'Tasks' })).toBeInTheDocument();
    await user.click(document.body);
    expect(screen.queryByRole('button', { name: 'Tasks' })).not.toBeInTheDocument();
  });

  it('preserves focus and shared pressed states across desktop and mobile controls', async () => {
    const props = toolbarProps();
    const onFocusProjectsChange = vi.fn();
    const onIncludeSharedChange = vi.fn();
    const { user } = render(
      <GraphToolbar
        {...props}
        focusAvailable
        focusProjects
        focusedProjectCount={2}
        onFocusProjectsChange={onFocusProjectsChange}
        sharedAvailable
        includeShared
        sharedLabel="Shared"
        onIncludeSharedChange={onIncludeSharedChange}
      />
    );

    const focusButtons = screen.getAllByTitle('Show all projects in graph');
    const sharedButtons = screen.getAllByTitle('Hide shared knowledge');
    expect(focusButtons).toHaveLength(2);
    expect(sharedButtons).toHaveLength(2);
    expect(focusButtons.every(button => button.getAttribute('aria-pressed') === 'true')).toBe(true);
    expect(sharedButtons.every(button => button.getAttribute('aria-pressed') === 'true')).toBe(
      true
    );

    await user.click(focusButtons[0]);
    await user.click(sharedButtons[0]);
    expect(onFocusProjectsChange).toHaveBeenCalledWith(false);
    expect(onIncludeSharedChange).toHaveBeenCalledWith(false);
  });
});
