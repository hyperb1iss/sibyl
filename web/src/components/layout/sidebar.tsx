'use client';

import { AnimatePresence, motion } from 'motion/react';
import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect } from 'react';
import {
  BookOpen,
  Boxes,
  FolderKanban,
  type IconComponent,
  LayoutDashboard,
  ListTodo,
  Loader2,
  Network,
  RefreshCw,
  Search,
  Sparkles,
  Wifi,
  WifiOff,
  X,
} from '@/components/ui/icons';
import { APP_CONFIG } from '@/lib/constants';
import { useConnectionStatus, useHealth } from '@/lib/hooks';
import { useMobileNav } from './mobile-nav-context';
import { NavLink } from './nav-link';

// Navigation with Iconoir icons
const NAVIGATION: Array<{ name: string; href: string; icon: IconComponent }> = [
  { name: 'Dashboard', href: '/', icon: LayoutDashboard },
  { name: 'Projects', href: '/projects', icon: FolderKanban },
  { name: 'Tasks', href: '/tasks', icon: ListTodo },
  { name: 'Sources', href: '/sources', icon: BookOpen },
  { name: 'Graph', href: '/graph', icon: Network },
  { name: 'Entities', href: '/entities', icon: Boxes },
  { name: 'Search', href: '/search', icon: Search },
  { name: 'Ingest', href: '/ingest', icon: RefreshCw },
];

interface SidebarContentProps {
  onNavClick?: () => void;
  isConnected: boolean;
  isReconnecting: boolean;
}

function SidebarContent({ onNavClick, isConnected, isReconnecting }: SidebarContentProps) {
  const StatusIcon = isConnected ? Wifi : isReconnecting ? Loader2 : WifiOff;
  const statusLabel = isConnected ? 'Live' : isReconnecting ? 'Syncing' : 'Offline';
  const statusColor = isConnected ? 'sc-green' : isReconnecting ? 'sc-yellow' : 'sc-red';

  return (
    <>
      {/* Logo */}
      <div className="p-4 md:p-6 border-b border-sc-fg-subtle/10">
        <Link href="/" className="flex items-center gap-3 group" onClick={onNavClick}>
          <div className="relative">
            {/* Glow effect */}
            <div className="absolute inset-0 rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral blur-lg opacity-50 group-hover:opacity-75 transition-opacity" />
            {/* Logo container */}
            <div className="relative w-10 h-10 rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral flex items-center justify-center shadow-lg">
              <Sparkles width={20} height={20} className="text-white" />
            </div>
          </div>
          <div>
            <h1 className="text-lg font-bold text-sc-fg-primary tracking-tight">Sibyl</h1>
            <p className="text-[10px] text-sc-fg-subtle uppercase tracking-widest">
              Knowledge Oracle
            </p>
          </div>
        </Link>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-3 md:p-4 space-y-1 overflow-y-auto">
        {NAVIGATION.map(item => (
          <NavLink key={item.name} href={item.href} icon={item.icon} onClick={onNavClick}>
            {item.name}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="p-3 md:p-4 border-t border-sc-fg-subtle/10">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 text-[10px] text-sc-fg-subtle">
            <span className="uppercase tracking-wider">
              {APP_CONFIG.NAME} v{APP_CONFIG.VERSION}
            </span>
          </div>
          <div
            className={`flex items-center gap-1.5 px-2 py-1 rounded-full text-[10px] font-medium uppercase tracking-wide text-${statusColor}`}
          >
            <StatusIcon width={12} height={12} className={isReconnecting ? 'animate-spin' : ''} />
            <span>{statusLabel}</span>
          </div>
        </div>
      </div>
    </>
  );
}

export function Sidebar() {
  const { isOpen, close } = useMobileNav();
  const _pathname = usePathname();
  const { data: health } = useHealth();
  const wsStatus = useConnectionStatus();

  // Determine overall connection state
  const isConnected = health?.status === 'healthy' && wsStatus === 'connected';
  const isReconnecting = wsStatus === 'reconnecting' || wsStatus === 'connecting';

  // Close mobile nav on route change
  useEffect(() => {
    close();
  }, [close]);

  // Close on escape key
  useEffect(() => {
    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isOpen) {
        close();
      }
    };
    document.addEventListener('keydown', handleEscape);
    return () => document.removeEventListener('keydown', handleEscape);
  }, [isOpen, close]);

  return (
    <>
      {/* Desktop Sidebar - hidden on mobile */}
      <aside className="hidden md:flex w-64 bg-sc-bg-base border-r border-sc-fg-subtle/10 flex-col">
        <SidebarContent isConnected={isConnected} isReconnecting={isReconnecting} />
      </aside>

      {/* Mobile Drawer */}
      <AnimatePresence>
        {isOpen && (
          <>
            {/* Backdrop */}
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.2 }}
              className="fixed inset-0 bg-black/60 backdrop-blur-sm z-40 md:hidden"
              onClick={close}
              aria-hidden="true"
            />

            {/* Drawer */}
            <motion.aside
              initial={{ x: '-100%' }}
              animate={{ x: 0 }}
              exit={{ x: '-100%' }}
              transition={{ type: 'spring', damping: 25, stiffness: 300 }}
              className="fixed inset-y-0 left-0 w-72 bg-sc-bg-base border-r border-sc-fg-subtle/10 flex flex-col z-50 md:hidden shadow-2xl shadow-black/50"
            >
              {/* Close button */}
              <button
                type="button"
                onClick={close}
                className="absolute top-4 right-4 p-2 rounded-lg text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight transition-colors"
                aria-label="Close navigation"
              >
                <X width={20} height={20} />
              </button>

              <SidebarContent
                onNavClick={close}
                isConnected={isConnected}
                isReconnecting={isReconnecting}
              />
            </motion.aside>
          </>
        )}
      </AnimatePresence>
    </>
  );
}
