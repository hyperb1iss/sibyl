'use client';

import {
  keepPreviousData,
  MutationCache,
  QueryClient,
  QueryClientProvider,
} from '@tanstack/react-query';
import { usePathname } from 'next/navigation';
import { type ReactNode, Suspense, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { ThemedToaster } from '@/components/ui/themed-toaster';
import { printConsoleGreeting } from '@/lib/console-greeting';
import { useMe, useRealtimeUpdates } from '@/lib/hooks';
import { ProjectContextProvider } from '@/lib/project-context';
import { isPublicRoutePath } from '@/lib/public-routes';
import { ThemeProvider } from '@/lib/theme';

function RealtimeProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const isPublicRoute = isPublicRoutePath(pathname);
  const { data: me, isSuccess } = useMe({ enabled: !isPublicRoute });
  const isAuthenticated = isPublicRoute ? false : isSuccess ? !!me?.user : undefined;
  useRealtimeUpdates(isAuthenticated);
  return <>{children}</>;
}

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        mutationCache: new MutationCache({
          onError: (error: Error) => {
            // Extract error message from API response or use generic message
            const message =
              error.message && error.message !== 'An error occurred'
                ? error.message
                : 'Something went wrong. Please try again.';
            toast.error(message);
          },
        }),
        defaultOptions: {
          queries: {
            staleTime: 60 * 1000, // 1 minute
            gcTime: 5 * 60 * 1000, // 5 minutes
            retry: 1,
            // Stale-while-revalidate: don't tear down the UI when the user
            // tabs back in or reconnects. Realtime/websocket invalidation
            // is the primary freshness signal; window-focus refetch causes
            // visible reload churn on every tab switch.
            refetchOnWindowFocus: false,
            refetchOnReconnect: false,
            // Keep showing previous data while a refetch is in flight so
            // the page doesn't flash a skeleton on background updates or
            // when a query key changes (e.g. selected project, filter).
            placeholderData: keepPreviousData,
          },
        },
      })
  );

  useEffect(() => {
    printConsoleGreeting();
  }, []);

  return (
    <ThemeProvider>
      <QueryClientProvider client={queryClient}>
        <Suspense fallback={null}>
          <ProjectContextProvider>
            <RealtimeProvider>{children}</RealtimeProvider>
          </ProjectContextProvider>
        </Suspense>
        <ThemedToaster />
      </QueryClientProvider>
    </ThemeProvider>
  );
}
