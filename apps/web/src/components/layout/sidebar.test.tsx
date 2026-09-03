import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@/test/utils';
import { MobileNavProvider } from './mobile-nav-context';
import { Sidebar } from './sidebar';
import { SIDEBAR_COLLAPSED_STORAGE_KEY } from './sidebar-collapse';

function renderSidebar() {
  return render(
    <MobileNavProvider>
      <Sidebar />
    </MobileNavProvider>
  );
}

function desktopRail() {
  const rail = document.getElementById('app-sidebar');
  if (!rail) throw new Error('desktop sidebar rail not rendered');
  return rail;
}

// jsdom under Node 24 exposes no usable localStorage in this runner, so the
// suite stubs an in-memory Storage the same way welcome-banner.test does.
function createMemoryStorage(): Storage {
  const store = new Map<string, string>();
  return {
    get length() {
      return store.size;
    },
    clear: () => store.clear(),
    getItem: (key: string) => store.get(key) ?? null,
    key: (index: number) => [...store.keys()][index] ?? null,
    removeItem: (key: string) => {
      store.delete(key);
    },
    setItem: (key: string, value: string) => {
      store.set(key, String(value));
    },
  };
}

describe('Sidebar', () => {
  beforeEach(() => {
    vi.stubGlobal('localStorage', createMemoryStorage());
    document.documentElement.removeAttribute('data-sidebar');
    document.documentElement.removeAttribute('data-sidebar-hydrated');
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('does not render a capture launcher in navigation chrome', () => {
    renderSidebar();

    expect(screen.queryByRole('button', { name: /capture memory/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/quick capture/i)).not.toBeInTheDocument();
  });

  it('groups navigation into labeled sections', () => {
    renderSidebar();
    const rail = within(desktopRail());

    for (const label of ['Overview', 'Work', 'Memory', 'Explore']) {
      expect(rail.getByRole('group', { name: label })).toBeInTheDocument();
    }
    expect(rail.getByText('Work')).toBeInTheDocument();
    expect(within(rail.getByRole('group', { name: 'Work' })).getAllByRole('link')).toHaveLength(3);
    expect(
      within(rail.getByRole('group', { name: 'Memory' }))
        .getAllByRole('link')
        .map(link => link.textContent)
    ).toEqual(['Memory', 'Sources', 'Entities', 'Graph']);
  });

  it('puts settings in the footer without project context', () => {
    renderSidebar();
    const rail = within(desktopRail());

    const settings = rail.getByRole('link', { name: 'Settings' });
    expect(settings).toHaveAttribute('href', '/settings');
    expect(rail.getByRole('group', { name: 'System' })).toContainElement(settings);
  });

  it('collapses to an icon rail, persists, and marks the document as hydrated', async () => {
    const { user } = renderSidebar();
    const rail = desktopRail();

    expect(rail).not.toHaveAttribute('data-collapsed');
    expect(document.documentElement).toHaveAttribute('data-sidebar-hydrated');

    await user.click(screen.getByRole('button', { name: 'Collapse sidebar' }));

    expect(rail).toHaveAttribute('data-collapsed');
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('true');
    expect(document.documentElement).toHaveAttribute('data-sidebar', 'collapsed');
    // Section labels leave the DOM in rail mode; links keep an accessible name.
    expect(within(rail).queryByText('Work')).not.toBeInTheDocument();
    expect(within(rail).getByRole('link', { name: 'Tasks' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Expand sidebar' }));
    expect(rail).not.toHaveAttribute('data-collapsed');
    expect(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY)).toBe('false');
  });

  it('restores the persisted collapsed state on mount', () => {
    window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, 'true');
    renderSidebar();

    expect(desktopRail()).toHaveAttribute('data-collapsed');
    expect(screen.getByRole('button', { name: 'Expand sidebar' })).toBeInTheDocument();
  });

  it('toggles the rail from the keyboard with Cmd+B and [', async () => {
    const { user } = renderSidebar();
    const rail = desktopRail();

    await user.keyboard('{Meta>}b{/Meta}');
    expect(rail).toHaveAttribute('data-collapsed');

    await user.keyboard('[[');
    expect(rail).not.toHaveAttribute('data-collapsed');
  });
});
