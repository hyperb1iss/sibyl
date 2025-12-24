import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { type RenderOptions, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement, ReactNode } from 'react';

/**
 * Create a fresh QueryClient for tests with sensible defaults.
 */
export function createTestQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        gcTime: 0,
        staleTime: 0,
      },
      mutations: {
        retry: false,
      },
    },
  });
}

interface WrapperProps {
  children: ReactNode;
}

/**
 * Test wrapper with all required providers.
 */
function createWrapper(queryClient?: QueryClient) {
  const client = queryClient ?? createTestQueryClient();

  return function Wrapper({ children }: WrapperProps) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  };
}

/**
 * Custom render that includes providers and userEvent setup.
 */
export function renderWithProviders(
  ui: ReactElement,
  options?: Omit<RenderOptions, 'wrapper'> & { queryClient?: QueryClient }
) {
  const { queryClient, ...renderOptions } = options ?? {};

  return {
    user: userEvent.setup(),
    queryClient: queryClient ?? createTestQueryClient(),
    ...render(ui, {
      wrapper: createWrapper(queryClient),
      ...renderOptions,
    }),
  };
}

// Re-export everything from testing-library
export * from '@testing-library/react';
export { userEvent };

// Default render override
export { renderWithProviders as render };
