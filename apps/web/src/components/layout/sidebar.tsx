'use client';

import { AnimatePresence, motion } from 'motion/react';
import Image from 'next/image';
import Link from 'next/link';
import { useEffect } from 'react';
import { GradientButton } from '@/components/ui/button';
import { X } from '@/components/ui/icons';
import { APP_CONFIG, NAVIGATION } from '@/lib/constants';
import { SIDEBAR_CAPTURE_CTA } from '@/lib/constants/navigation';
import { useCaptureMemory } from './capture-memory-context';
import { useMobileNav } from './mobile-nav-context';
import { NavLink } from './nav-link';

interface SidebarContentProps {
  onNavClick?: () => void;
}

function SidebarContent({ onNavClick }: SidebarContentProps) {
  const { openCaptureMemory } = useCaptureMemory();

  const handleCaptureClick = () => {
    onNavClick?.();
    openCaptureMemory(SIDEBAR_CAPTURE_CTA.surface);
  };

  return (
    <>
      {/* Logo */}
      <div className="py-4 pr-4 pl-0 md:py-6 md:pr-6 md:pl-0 border-b border-sc-fg-subtle/10">
        <Link href="/" className="block text-center" onClick={onNavClick}>
          <Image
            src="/sibyl-logo.png"
            alt="Sibyl"
            width={180}
            height={52}
            className="h-14 w-auto mx-auto animate-logo-glow"
            priority
          />
          <div className="mt-1.5 text-center">
            <p className="tagline text-[10px] uppercase tracking-[0.08em] font-medium">
              <span className="tagline-word">Collective</span>
              <span className="tagline-separator mx-1 opacity-50">·</span>
              <span className="tagline-word">Intelligence</span>
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

      <div className="px-3 pb-3 md:px-4 md:pb-4">
        <div className="rounded-2xl border border-sc-purple/20 bg-gradient-to-br from-sc-purple/10 via-sc-bg-highlight to-sc-cyan/10 p-3 shadow-[0_0_28px_rgba(225,53,255,0.12)]">
          <p className="text-[10px] uppercase tracking-[0.16em] text-sc-purple/80">Quick capture</p>
          <p className="mt-1 text-sm font-semibold text-sc-fg-primary">
            {SIDEBAR_CAPTURE_CTA.label}
          </p>
          <p className="mt-1 text-xs leading-5 text-sc-fg-muted">
            {SIDEBAR_CAPTURE_CTA.description}
          </p>
          <GradientButton
            gradient="purple-cyan"
            size="sm"
            className="mt-3 w-full justify-start"
            icon={<SIDEBAR_CAPTURE_CTA.icon width={16} height={16} />}
            onClick={handleCaptureClick}
            aria-label={SIDEBAR_CAPTURE_CTA.label}
          >
            {SIDEBAR_CAPTURE_CTA.label}
          </GradientButton>
        </div>
      </div>

      {/* Footer */}
      <div className="p-3 md:p-4 border-t border-sc-fg-subtle/10">
        <div className="flex items-center justify-center text-[10px] text-sc-fg-subtle">
          <span className="uppercase tracking-wider">
            {APP_CONFIG.NAME} v{APP_CONFIG.VERSION}
          </span>
        </div>
      </div>
    </>
  );
}

export function Sidebar() {
  const { isOpen, close } = useMobileNav();

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
      <aside className="hidden md:flex w-64 bg-sc-bg-base border-r border-sc-fg-subtle/10 flex-col shadow-sidebar">
        <SidebarContent />
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

              <SidebarContent onNavClick={close} />
            </motion.aside>
          </>
        )}
      </AnimatePresence>
    </>
  );
}
