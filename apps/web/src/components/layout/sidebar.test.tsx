import { describe, expect, it } from 'vitest';
import { render, screen } from '@/test/utils';
import { MobileNavProvider } from './mobile-nav-context';
import { Sidebar } from './sidebar';

describe('Sidebar', () => {
  it('does not render a capture launcher in navigation chrome', () => {
    render(
      <MobileNavProvider>
        <Sidebar />
      </MobileNavProvider>
    );

    expect(screen.queryByRole('button', { name: /capture memory/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/quick capture/i)).not.toBeInTheDocument();
  });
});
