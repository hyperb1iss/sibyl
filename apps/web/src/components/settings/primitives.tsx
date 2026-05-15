'use client';

import type { ReactNode } from 'react';
import type { IconComponent } from '@/components/ui/icons';
import { Database, Globe, InfoCircle, Key } from '@/components/ui/icons';
import { Tooltip } from '@/components/ui/tooltip';

interface SettingsPageHeaderProps {
  icon?: IconComponent;
  iconColor?: string;
  title: string;
  description?: string;
  actions?: ReactNode;
}

export function SettingsPageHeader({
  icon: Icon,
  iconColor = 'text-sc-purple',
  title,
  description,
  actions,
}: SettingsPageHeaderProps) {
  return (
    <header className="flex flex-col gap-3 pb-5 border-b border-sc-fg-subtle/10 sm:flex-row sm:items-start sm:justify-between">
      <div className="flex items-start gap-3 min-w-0">
        {Icon && (
          <span
            className={`mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-sc-bg-highlight ${iconColor}`}
            aria-hidden="true"
          >
            <Icon width={18} height={18} />
          </span>
        )}
        <div className="min-w-0">
          <h1 className="text-xl font-semibold text-sc-fg-primary leading-tight">{title}</h1>
          {description && (
            <p className="mt-1 text-sm text-sc-fg-muted leading-relaxed">{description}</p>
          )}
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
    </header>
  );
}

interface SettingsSectionProps {
  title?: string;
  description?: string;
  icon?: IconComponent;
  iconColor?: string;
  actions?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  /** Pull children flush against the card edge (e.g., row lists). */
  flush?: boolean;
  className?: string;
}

export function SettingsSection({
  title,
  description,
  icon: Icon,
  iconColor = 'text-sc-cyan',
  actions,
  aside,
  children,
  flush = false,
  className = '',
}: SettingsSectionProps) {
  const showHeader = !!(title || description || actions || aside);
  return (
    <section className={`rounded-lg border border-sc-fg-subtle/10 bg-sc-bg-base ${className}`}>
      {showHeader && (
        <div className="flex flex-col gap-3 px-6 py-4 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex items-start gap-2.5 min-w-0">
            {Icon && <Icon width={16} height={16} className={`mt-1 shrink-0 ${iconColor}`} />}
            <div className="min-w-0">
              {title && (
                <h2 className="text-sm font-semibold uppercase tracking-[0.08em] text-sc-fg-secondary">
                  {title}
                </h2>
              )}
              {description && (
                <p className="mt-1 text-sm text-sc-fg-muted leading-relaxed">{description}</p>
              )}
            </div>
          </div>
          {(actions || aside) && (
            <div className="flex shrink-0 items-center gap-2 self-start sm:self-center">
              {aside}
              {actions}
            </div>
          )}
        </div>
      )}
      <div className={flush ? '' : 'px-6 pb-5 pt-1'}>
        {showHeader && !flush && <div className="border-t border-sc-fg-subtle/5 mb-5" />}
        {children}
      </div>
    </section>
  );
}

interface SettingsRowProps {
  label: string;
  description?: string;
  control: ReactNode;
  /** Stack control beneath the label on narrow screens (default true). */
  stack?: boolean;
  /** Render a subtle divider beneath. */
  divider?: boolean;
}

export function SettingsRow({
  label,
  description,
  control,
  stack = true,
  divider = false,
}: SettingsRowProps) {
  return (
    <div
      className={`flex flex-col gap-3 py-4 first:pt-0 last:pb-0 ${
        stack ? 'sm:flex-row sm:items-center sm:justify-between' : ''
      } ${divider ? 'border-b border-sc-fg-subtle/5' : ''}`}
    >
      <div className="min-w-0 sm:pr-6">
        <p className="text-sm font-medium text-sc-fg-primary">{label}</p>
        {description && <p className="mt-0.5 text-xs text-sc-fg-muted">{description}</p>}
      </div>
      <div className="shrink-0">{control}</div>
    </div>
  );
}

export type SettingsFieldSource = 'env' | 'db' | 'default' | 'none';

interface SettingsFieldProps {
  label: string;
  hint?: string;
  source?: SettingsFieldSource;
  locked?: boolean;
  envVar?: string | null;
  htmlFor?: string;
  children: ReactNode;
  className?: string;
}

export function SettingsField({
  label,
  hint,
  source,
  locked,
  envVar,
  htmlFor,
  children,
  className = '',
}: SettingsFieldProps) {
  return (
    <div className={`block ${className}`}>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <label
          htmlFor={htmlFor}
          className="text-[11px] font-medium uppercase tracking-[0.06em] text-sc-fg-subtle"
        >
          {label}
        </label>
        {source && source !== 'none' && (
          <SourceIndicator source={source} locked={locked} envVar={envVar} />
        )}
      </div>
      {children}
      {hint && <p className="mt-1.5 text-xs text-sc-fg-muted">{hint}</p>}
    </div>
  );
}

interface SourceIndicatorProps {
  source: SettingsFieldSource;
  locked?: boolean;
  envVar?: string | null;
}

export function SourceIndicator({ source, locked, envVar }: SourceIndicatorProps) {
  if (source === 'none') return null;

  let Icon: IconComponent = InfoCircle;
  let color = 'text-sc-fg-subtle';
  let label = 'Default value';

  if (source === 'env') {
    Icon = locked ? Key : Globe;
    color = 'text-sc-purple';
    label = locked && envVar ? `Locked by ${envVar}` : 'Set via environment variable';
  } else if (source === 'db') {
    Icon = Database;
    color = 'text-sc-cyan';
    label = 'Stored in database';
  }

  return (
    <Tooltip content={label}>
      <span
        className={`inline-flex h-4 w-4 items-center justify-center ${color}`}
        role="img"
        aria-label={label}
      >
        <Icon width={11} height={11} />
      </span>
    </Tooltip>
  );
}

interface HelpNoteProps {
  children: ReactNode;
  tone?: 'muted' | 'warning' | 'info';
  icon?: IconComponent;
}

export function HelpNote({ children, tone = 'muted', icon: Icon }: HelpNoteProps) {
  const palette = {
    muted: 'border-sc-fg-subtle/10 bg-sc-bg-highlight/40 text-sc-fg-muted',
    warning: 'border-sc-yellow/20 bg-sc-yellow/5 text-sc-fg-secondary',
    info: 'border-sc-cyan/20 bg-sc-cyan/5 text-sc-fg-secondary',
  }[tone];

  const iconColor = {
    muted: 'text-sc-fg-subtle',
    warning: 'text-sc-yellow',
    info: 'text-sc-cyan',
  }[tone];

  return (
    <div className={`flex items-start gap-2.5 rounded-lg border px-3 py-2.5 text-sm ${palette}`}>
      {Icon && <Icon width={14} height={14} className={`mt-0.5 shrink-0 ${iconColor}`} />}
      <div className="leading-relaxed">{children}</div>
    </div>
  );
}

type StatusTone = 'success' | 'danger' | 'warning' | 'neutral' | 'info';

interface StatusPillProps {
  tone: StatusTone;
  icon?: IconComponent;
  children: ReactNode;
}

export function StatusPill({ tone, icon: Icon, children }: StatusPillProps) {
  const palette = {
    success: 'border-sc-green/30 bg-sc-green/10 text-sc-green',
    danger: 'border-sc-red/30 bg-sc-red/10 text-sc-red',
    warning: 'border-sc-yellow/30 bg-sc-yellow/10 text-sc-yellow',
    neutral: 'border-sc-fg-subtle/20 bg-sc-bg-highlight text-sc-fg-muted',
    info: 'border-sc-cyan/30 bg-sc-cyan/10 text-sc-cyan',
  }[tone];

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-[11px] font-medium ${palette}`}
    >
      {Icon && <Icon width={11} height={11} />}
      {children}
    </span>
  );
}
