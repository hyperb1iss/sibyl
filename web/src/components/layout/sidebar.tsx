import Link from 'next/link';

import { NavLink } from './nav-link';
import { APP_CONFIG, NAVIGATION } from '@/lib/constants';

export function Sidebar() {
  return (
    <aside className="w-64 bg-sc-bg-base border-r border-sc-fg-subtle/20 flex flex-col">
      {/* Logo */}
      <div className="p-6 border-b border-sc-fg-subtle/20">
        <Link href="/" className="flex items-center gap-3 group">
          <div className="w-10 h-10 rounded-xl bg-gradient-to-br from-sc-purple via-sc-magenta to-sc-coral flex items-center justify-center text-white font-bold text-lg shadow-lg shadow-sc-purple/30 group-hover:shadow-sc-purple/50 transition-shadow">
            S
          </div>
          <div>
            <h1 className="text-lg font-semibold text-sc-fg-primary tracking-tight">Sibyl</h1>
            <p className="text-xs text-sc-fg-muted">Knowledge Oracle</p>
          </div>
        </Link>
      </div>

      {/* Navigation */}
      <nav className="flex-1 p-4 space-y-1">
        {NAVIGATION.map((item) => (
          <NavLink key={item.name} href={item.href} icon={item.icon}>
            {item.name}
          </NavLink>
        ))}
      </nav>

      {/* Footer */}
      <div className="p-4 border-t border-sc-fg-subtle/20">
        <div className="text-xs text-sc-fg-subtle space-y-1">
          <p className="flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-sc-green animate-pulse" />
            {APP_CONFIG.NAME} v{APP_CONFIG.VERSION}
          </p>
          <p className="text-sc-cyan font-mono">localhost:3334</p>
        </div>
      </div>
    </aside>
  );
}
