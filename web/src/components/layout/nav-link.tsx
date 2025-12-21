'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import type { ReactNode } from 'react';

interface NavLinkProps {
  href: string;
  icon: ReactNode;
  children: ReactNode;
}

export function NavLink({ href, icon, children }: NavLinkProps) {
  const pathname = usePathname();
  const isActive = pathname === href;

  return (
    <Link
      href={href}
      className={`
        flex items-center gap-3 px-4 py-3 rounded-lg transition-all duration-150
        ${isActive
          ? 'bg-sc-purple/20 text-sc-purple shadow-inner'
          : 'text-sc-fg-muted hover:text-sc-fg-primary hover:bg-sc-bg-highlight hover:translate-x-1'
        }
      `}
    >
      <span className={`text-lg ${isActive ? 'animate-pulse' : ''}`}>{icon}</span>
      <span className="font-medium">{children}</span>
      {isActive && (
        <span className="ml-auto w-1.5 h-1.5 rounded-full bg-sc-purple animate-pulse" />
      )}
    </Link>
  );
}
