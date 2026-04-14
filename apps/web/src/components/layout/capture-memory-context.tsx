'use client';

import { createContext, type ReactNode, useCallback, useContext, useState } from 'react';

interface CaptureMemoryContextValue {
  isOpen: boolean;
  captureSurface: string;
  openCaptureMemory: (surface?: string) => void;
  closeCaptureMemory: () => void;
}

const CaptureMemoryContext = createContext<CaptureMemoryContextValue | null>(null);

export function CaptureMemoryProvider({ children }: { children: ReactNode }) {
  const [isOpen, setIsOpen] = useState(false);
  const [captureSurface, setCaptureSurface] = useState('shell');

  const openCaptureMemory = useCallback((surface = 'shell') => {
    setCaptureSurface(surface);
    setIsOpen(true);
  }, []);

  const closeCaptureMemory = useCallback(() => {
    setIsOpen(false);
  }, []);

  return (
    <CaptureMemoryContext.Provider
      value={{ isOpen, captureSurface, openCaptureMemory, closeCaptureMemory }}
    >
      {children}
    </CaptureMemoryContext.Provider>
  );
}

export function useCaptureMemory() {
  const context = useContext(CaptureMemoryContext);
  if (!context) {
    throw new Error('useCaptureMemory must be used within a CaptureMemoryProvider');
  }
  return context;
}
