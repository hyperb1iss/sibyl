'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { type ReactNode, useState } from 'react';

import { useMe, useRealtimeUpdates } from '@/lib/hooks';

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

  return (
    <QueryClientProvider client={queryClient}>
      <RealtimeProvider>{children}</RealtimeProvider>
    </QueryClientProvider>
  );
}
