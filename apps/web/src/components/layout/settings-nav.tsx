'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import type { IconComponent } from '@/components/ui/icons';
import {
  Activity,
  Archive,
  ClipboardCheck,
  Database,
  Eye,
  Flash,
  Key,
  Settings,
  User,
  Users,
} from '@/components/ui/icons';
import { useMe } from '@/lib/hooks';

interface SettingsNavItem {
  name: string;
  href: string;
  icon: IconComponent;
  hint: string;
}

const ACCOUNT_NAVIGATION: SettingsNavItem[] = [
  { name: 'Profile', href: '/settings/profile', icon: User, hint: 'Your personal information' },
  {
    name: 'Preferences',
    href: '/settings/preferences',
    icon: Eye,
    hint: 'Display and behavior',
  },
  {
    name: 'Security',
    href: '/settings/security',
    icon: Key,
    hint: 'Password, sessions, API keys',
  },
];

const ORG_NAVIGATION: SettingsNavItem[] = [
  {
    name: 'Organizations',
    href: '/settings/organizations',
    icon: Users,
    hint: 'Manage your organizations',
  },
  { name: 'Teams', href: '/settings/teams', icon: Users, hint: 'Team membership' },
  { name: 'Data', href: '/settings/data', icon: Database, hint: 'Backup and restore' },
];

const ADMIN_NAVIGATION: SettingsNavItem[] = [
  {
    name: 'AI Services',
    href: '/settings/admin/ai',
    icon: Flash,
    hint: 'Keys, models, embeddings',
  },
  {
    name: 'Audit Log',
    href: '/settings/admin/audit',
    icon: ClipboardCheck,
    hint: 'Security and access events',
  },
  { name: 'Backups', href: '/settings/admin/backups', icon: Archive, hint: 'Scheduled backups' },
  {
    name: 'System',
    href: '/settings/admin/system',
    icon: Activity,
    hint: 'Health and diagnostics',
  },
];

interface NavItemProps {
  item: SettingsNavItem;
  active: boolean;
}

function NavItem({ item, active }: NavItemProps) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      title={item.hint}
      className={`group relative flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
        active
          ? 'bg-sc-purple/10 text-sc-purple'
          : 'text-sc-fg-muted hover:bg-sc-bg-highlight/60 hover:text-sc-fg-primary'
      }`}
    >
      {active && (
        <span
          aria-hidden="true"
          className="absolute -left-1 top-1/2 h-5 w-0.5 -translate-y-1/2 rounded-full bg-sc-purple shadow-[0_0_8px_color-mix(in_oklch,var(--sc-purple)_60%,transparent)]"
        />
      )}
      <Icon
        width={16}
        height={16}
        className={active ? 'text-sc-purple' : 'text-sc-cyan/60 group-hover:text-sc-cyan'}
      />
      <span>{item.name}</span>
    </Link>
  );
}

function NavSection({
  label,
  items,
  pathname,
}: {
  label?: string;
  items: SettingsNavItem[];
  pathname: string;
}) {
  return (
    <div className="space-y-0.5">
      {label && (
        <div className="px-3 pb-1.5 pt-3 text-[10px] font-semibold uppercase tracking-[0.12em] text-sc-fg-subtle">
          {label}
        </div>
      )}
      {items.map(item => (
        <NavItem
          key={item.href}
          item={item}
          active={pathname === item.href || pathname.startsWith(`${item.href}/`)}
        />
      ))}
    </div>
  );
}

export function SettingsNav() {
  const pathname = usePathname();
  const { data: me } = useMe();
  const userRole = me?.org_role;
  const isAdmin = userRole === 'owner' || userRole === 'admin';

  return (
    <nav aria-label="Settings navigation" className="space-y-2">
      <div className="px-3 pb-2">
        <div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.12em] text-sc-fg-subtle">
          <Settings width={12} height={12} />
          Settings
        </div>
      </div>
      <NavSection label="Account" items={ACCOUNT_NAVIGATION} pathname={pathname} />
      <NavSection label="Workspace" items={ORG_NAVIGATION} pathname={pathname} />
      {isAdmin && (
        <NavSection label="Administration" items={ADMIN_NAVIGATION} pathname={pathname} />
      )}
    </nav>
  );
}
