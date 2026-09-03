'use client';

import { AnimatePresence, animate, motion, useMotionValue } from 'motion/react';
import Image from 'next/image';
import Link from 'next/link';
import { useEffect } from 'react';
import { Database, Github, Heart, NavArrowLeft, X } from '@/components/ui/icons';
import { Tooltip } from '@/components/ui/tooltip';
import { APP_CONFIG } from '@/lib/constants/app';
import { NAVIGATION_SECTIONS } from '@/lib/constants/navigation';
import { isEditableTarget, isModifierChord } from '@/lib/keyboard';
import { useMobileNav } from './mobile-nav-context';
import { NavLink } from './nav-link';
import {
  SIDEBAR_COLLAPSED_WIDTH,
  SIDEBAR_EXPANDED_WIDTH,
  useSidebarCollapsed,
} from './sidebar-collapse';

const PRIMARY_SECTIONS = NAVIGATION_SECTIONS.filter(section => section.id !== 'system');
const SYSTEM_ITEMS = NAVIGATION_SECTIONS.find(section => section.id === 'system')?.items ?? [];

const FOOTER_ICON_LINK =
  'rounded-lg p-1.5 text-sc-fg-subtle transition-colors duration-200 hover:bg-sc-bg-highlight focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base';

interface SidebarContentProps {
  collapsed?: boolean;
  onNavClick?: () => void;
  onToggleCollapse?: () => void;
}

function SidebarLogo({
  collapsed,
  onNavClick,
}: Pick<SidebarContentProps, 'collapsed' | 'onNavClick'>) {
  if (collapsed) {
    return (
      <div className="flex justify-center border-b border-sc-fg-subtle/10 py-4">
        <Tooltip content="Sibyl" side="right">
          <Link
            href="/"
            onClick={onNavClick}
            aria-label="Sibyl home"
            className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral animate-logo-glow focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base"
          >
            <Database width={18} height={18} className="text-sc-on-accent" />
          </Link>
        </Tooltip>
      </div>
    );
  }

  return (
    <div className="py-4 pr-4 pl-0 md:py-5 md:pr-6 md:pl-0 border-b border-sc-fg-subtle/10">
      <Link href="/" className="block text-center" onClick={onNavClick}>
        <Image
          src="/sibyl-logo.png"
          alt="Sibyl"
          width={180}
          height={52}
          className="h-12 w-auto mx-auto animate-logo-glow"
          priority
        />
        <div className="mt-1 text-center">
          <p className="tagline text-[10px] uppercase tracking-[0.08em] font-medium whitespace-nowrap">
            <span className="tagline-word">Collective</span>
            <span className="tagline-separator mx-1 opacity-50">·</span>
            <span className="tagline-word">Intelligence</span>
          </p>
        </div>
      </Link>
    </div>
  );
}

function SidebarContent({ collapsed = false, onNavClick, onToggleCollapse }: SidebarContentProps) {
  return (
    <>
      <SidebarLogo collapsed={collapsed} onNavClick={onNavClick} />

      {/* Navigation */}
      <nav
        aria-label="Main navigation"
        className={`flex-1 overflow-y-auto overflow-x-hidden p-3 md:p-4 ${collapsed ? 'space-y-3' : 'space-y-4'}`}
      >
        {PRIMARY_SECTIONS.map((section, index) => (
          <div key={section.id} role="group" aria-label={section.label}>
            {collapsed ? (
              index > 0 && (
                <div aria-hidden="true" className="mx-2 mb-3 border-t border-sc-fg-subtle/10" />
              )
            ) : (
              <div className="px-3 pb-1.5 text-[10px] font-semibold uppercase tracking-wider text-sc-fg-subtle">
                {section.label}
              </div>
            )}
            <div className="space-y-1">
              {section.items.map(item => (
                <NavLink
                  key={item.href}
                  href={item.href}
                  icon={item.icon}
                  collapsed={collapsed}
                  onClick={onNavClick}
                >
                  {item.name}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>

      {/* Footer */}
      <div className="p-3 md:p-4 border-t border-sc-fg-subtle/10 space-y-2">
        {SYSTEM_ITEMS.length > 0 && (
          <div className="space-y-1" role="group" aria-label="System">
            {SYSTEM_ITEMS.map(item => (
              <NavLink
                key={item.href}
                href={item.href}
                icon={item.icon}
                collapsed={collapsed}
                preserveProjectsContext={false}
                onClick={onNavClick}
              >
                {item.name}
              </NavLink>
            ))}
          </div>
        )}

        <div className="flex items-center justify-center gap-1">
          <Tooltip content="GitHub" side={collapsed ? 'right' : 'top'}>
            <a
              href={APP_CONFIG.GITHUB_URL}
              target="_blank"
              rel="noopener noreferrer"
              className={`${FOOTER_ICON_LINK} hover:text-sc-cyan`}
              aria-label="Sibyl on GitHub"
            >
              <Github width={15} height={15} />
            </a>
          </Tooltip>
          <Tooltip content="Sponsor" side={collapsed ? 'right' : 'top'}>
            <a
              href={APP_CONFIG.SPONSOR_URL}
              target="_blank"
              rel="noopener noreferrer"
              className={`${FOOTER_ICON_LINK} hover:text-sc-coral`}
              aria-label="Sponsor Sibyl"
            >
              <Heart width={15} height={15} />
            </a>
          </Tooltip>
        </div>

        {!collapsed && (
          <div className="flex items-center justify-center text-[10px] text-sc-fg-muted">
            <span className="uppercase tracking-wider whitespace-nowrap">
              {APP_CONFIG.NAME} v{APP_CONFIG.VERSION}
            </span>
          </div>
        )}

        {onToggleCollapse && (
          <div className={`flex ${collapsed ? 'justify-center' : 'justify-end'}`}>
            <Tooltip
              content={collapsed ? 'Expand sidebar (⌘B)' : 'Collapse sidebar (⌘B)'}
              side="right"
            >
              <button
                type="button"
                onClick={onToggleCollapse}
                aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
                aria-expanded={!collapsed}
                aria-controls="app-sidebar"
                className={`${FOOTER_ICON_LINK} hover:text-sc-purple`}
              >
                <NavArrowLeft
                  width={16}
                  height={16}
                  className={`transition-transform duration-200 ${collapsed ? 'rotate-180' : ''}`}
                />
              </button>
            </Tooltip>
          </div>
        )}
      </div>
    </>
  );
}

export function Sidebar() {
  const { isOpen, close } = useMobileNav();
  const { collapsed, toggle, animate: animateWidth } = useSidebarCollapsed();
  const width = useMotionValue(SIDEBAR_EXPANDED_WIDTH);

  // Drive the rail width through a motion value rather than the declarative
  // animate prop: state-driven re-animation of the prop did not fire under
  // the React Compiler build, while a value bound to style always does.
  useEffect(() => {
    const controls = animate(width, collapsed ? SIDEBAR_COLLAPSED_WIDTH : SIDEBAR_EXPANDED_WIDTH, {
      duration: animateWidth ? 0.2 : 0,
      ease: 'easeOut',
    });
    return () => controls.stop();
  }, [animateWidth, collapsed, width]);

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

  // Cmd/Ctrl+B anywhere, or a bare "[" outside text fields, flips the rail.
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (isModifierChord(event) && !event.shiftKey && event.key.toLowerCase() === 'b') {
        event.preventDefault();
        toggle();
        return;
      }
      if (
        event.key === '[' &&
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey &&
        !isEditableTarget(event.target)
      ) {
        event.preventDefault();
        toggle();
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [toggle]);

  return (
    <>
      {/* Desktop Sidebar - hidden on mobile */}
      <motion.aside
        id="app-sidebar"
        data-sidebar-rail=""
        data-collapsed={collapsed ? '' : undefined}
        style={{ width }}
        className="hidden md:flex shrink-0 overflow-hidden bg-sc-bg-base border-r border-sc-fg-subtle/10 flex-col shadow-sidebar"
      >
        <div data-sidebar-content="" className="flex min-h-0 flex-1 flex-col">
          <SidebarContent collapsed={collapsed} onToggleCollapse={toggle} />
        </div>
      </motion.aside>

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
              className="fixed inset-0 bg-sc-bg-dark/80 backdrop-blur-sm z-40 md:hidden"
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
