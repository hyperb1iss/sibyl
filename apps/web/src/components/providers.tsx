'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { type ReactNode, Suspense, useEffect, useState } from 'react';
import { ThemedToaster } from '@/components/ui/themed-toaster';
import { printConsoleGreeting } from '@/lib/console-greeting';
import { useMe, useRealtimeUpdates } from '@/lib/hooks';
import { ProjectContextProvider } from '@/lib/project-context';
import { ThemeProvider } from '@/lib/theme';

function RealtimeProvider({ children }: { children: ReactNode }) {
  const { data: me, isSuccess } = useMe();
  const isAuthenticated = isSuccess && !!me?.user;
  useRealtimeUpdates(isAuthenticated);
  return <>{children}</>;
}

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 60 * 1000, // 1 minute
            gcTime: 5 * 60 * 1000, // 5 minutes
            retry: 1,
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
