'use client';

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { Eye, Globe, Network, Settings, Sparks } from '@/components/ui/icons';
import type { UserPreferences } from '@/lib/api';
import { usePreferences, useUpdatePreferences } from '@/lib/hooks';

function SectionSkeleton() {
  return (
    <div className="space-y-4 animate-pulse">
      {[1, 2, 3].map(i => (
        <div key={i} className="h-12 bg-sc-bg-highlight rounded-lg" />
      ))}
    </div>
  );
}

// =============================================================================
// Toggle Switch Component
// =============================================================================

interface ToggleProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}

function Toggle({ checked, onChange, disabled }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => !disabled && onChange(!checked)}
      disabled={disabled}
      className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-sc-purple focus:ring-offset-2 focus:ring-offset-sc-bg-dark ${
        checked ? 'bg-sc-purple' : 'bg-sc-fg-subtle/30'
      } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      <span
        className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out ${
          checked ? 'translate-x-5' : 'translate-x-0'
        }`}
      />
    </button>
  );
}

// =============================================================================
// Select Component
// =============================================================================

interface SelectOption {
  value: string;
  label: string;
}

interface SelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[];
  disabled?: boolean;
}

function Select({ value, onChange, options, disabled }: SelectProps) {
  return (
    <select
      value={value}
      onChange={e => onChange(e.target.value)}
      disabled={disabled}
      className="bg-sc-bg-highlight border border-sc-fg-subtle/20 rounded-lg px-3 py-2 text-sm text-sc-fg-primary focus:outline-none focus:ring-2 focus:ring-sc-purple disabled:opacity-50"
    >
      {options.map(opt => (
        <option key={opt.value} value={opt.value}>
          {opt.label}
        </option>
      ))}
    </select>
  );
}

// =============================================================================
// Appearance Section
// =============================================================================

interface SectionProps {
  prefs: UserPreferences;
  onUpdate: (updates: Partial<UserPreferences>) => void;
  isUpdating: boolean;
}

function AppearanceSection({ prefs, onUpdate, isUpdating }: SectionProps) {
  const themes: SelectOption[] = [
    { value: 'system', label: 'System Default' },
    { value: 'dark', label: 'Dark' },
    { value: 'light', label: 'Light' },
  ];

  return (
    <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
      <div className="flex items-center gap-3 mb-4">
        <Eye width={18} height={18} className="text-sc-purple" />
        <h3 className="font-semibold text-sc-fg-primary">Appearance</h3>
      </div>

      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-sc-fg-primary">Theme</p>
            <p className="text-xs text-sc-fg-muted">Choose your preferred color theme</p>
          </div>
          <Select
            value={prefs.theme || 'system'}
            onChange={v => onUpdate({ theme: v as 'light' | 'dark' | 'system' })}
            options={themes}
            disabled={isUpdating}
          />
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Locale Section
// =============================================================================

function LocaleSection({ prefs, onUpdate, isUpdating }: SectionProps) {
  const locales: SelectOption[] = [
    { value: 'en', label: 'English' },
    { value: 'es', label: 'Español' },
    { value: 'fr', label: 'Français' },
    { value: 'de', label: 'Deutsch' },
    { value: 'ja', label: '日本語' },
    { value: 'zh', label: '中文' },
  ];

  const timezones: SelectOption[] = [
    { value: 'auto', label: 'Auto-detect' },
    { value: 'America/Los_Angeles', label: 'Pacific Time (PT)' },
    { value: 'America/Denver', label: 'Mountain Time (MT)' },
    { value: 'America/Chicago', label: 'Central Time (CT)' },
    { value: 'America/New_York', label: 'Eastern Time (ET)' },
    { value: 'Europe/London', label: 'London (GMT/BST)' },
    { value: 'Europe/Paris', label: 'Paris (CET)' },
    { value: 'Asia/Tokyo', label: 'Tokyo (JST)' },
    { value: 'Asia/Shanghai', label: 'Shanghai (CST)' },
    { value: 'Australia/Sydney', label: 'Sydney (AEST)' },
  ];

  return (
    <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
      <div className="flex items-center gap-3 mb-4">
        <Globe width={18} height={18} className="text-sc-cyan" />
        <h3 className="font-semibold text-sc-fg-primary">Language & Region</h3>
      </div>

      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-sc-fg-primary">Language</p>
            <p className="text-xs text-sc-fg-muted">Select your preferred language</p>
          </div>
          <Select
            value={prefs.locale || 'en'}
            onChange={v => onUpdate({ locale: v })}
            options={locales}
            disabled={isUpdating}
          />
        </div>

        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-sc-fg-primary">Timezone</p>
            <p className="text-xs text-sc-fg-muted">Used for displaying dates and times</p>
          </div>
          <Select
            value={prefs.timezone || 'auto'}
            onChange={v => onUpdate({ timezone: v })}
            options={timezones}
            disabled={isUpdating}
          />
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Graph Section
// =============================================================================

function GraphSection({ prefs, onUpdate, isUpdating }: SectionProps) {
  return (
    <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
      <div className="flex items-center gap-3 mb-4">
        <Network width={18} height={18} className="text-sc-coral" />
        <h3 className="font-semibold text-sc-fg-primary">Knowledge Graph</h3>
      </div>

      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-sc-fg-primary">Show Labels</p>
            <p className="text-xs text-sc-fg-muted">Display labels on graph nodes by default</p>
          </div>
          <Toggle
            checked={prefs.graphShowLabels ?? true}
            onChange={v => onUpdate({ graphShowLabels: v })}
            disabled={isUpdating}
          />
        </div>

        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-sc-fg-primary">Default Zoom Level</p>
            <p className="text-xs text-sc-fg-muted">Initial zoom when opening the graph</p>
          </div>
          <Select
            value={String(prefs.graphDefaultZoom || 1)}
            onChange={v => onUpdate({ graphDefaultZoom: parseFloat(v) })}
            options={[
              { value: '0.5', label: '50%' },
              { value: '0.75', label: '75%' },
              { value: '1', label: '100%' },
              { value: '1.5', label: '150%' },
              { value: '2', label: '200%' },
            ]}
            disabled={isUpdating}
          />
        </div>

        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-sc-fg-primary">Default Dashboard View</p>
            <p className="text-xs text-sc-fg-muted">Layout for dashboard and entity lists</p>
          </div>
          <Select
            value={prefs.dashboardDefaultView || 'grid'}
            onChange={v => onUpdate({ dashboardDefaultView: v as 'grid' | 'list' })}
            options={[
              { value: 'grid', label: 'Grid' },
              { value: 'list', label: 'List' },
            ]}
            disabled={isUpdating}
          />
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Notifications Section
// =============================================================================

function NotificationsSection({ prefs, onUpdate, isUpdating }: SectionProps) {
  return (
    <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
      <div className="flex items-center gap-3 mb-4">
        <Sparks width={18} height={18} className="text-sc-yellow" />
        <h3 className="font-semibold text-sc-fg-primary">Notifications</h3>
      </div>

      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-sc-fg-primary">Task Assignments</p>
            <p className="text-xs text-sc-fg-muted">Notify when tasks are assigned to you</p>
          </div>
          <Toggle
            checked={prefs.notifyOnTaskAssigned ?? true}
            onChange={v => onUpdate({ notifyOnTaskAssigned: v })}
            disabled={isUpdating}
          />
        </div>

        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-medium text-sc-fg-primary">Mentions</p>
            <p className="text-xs text-sc-fg-muted">Notify when you are mentioned</p>
          </div>
          <Toggle
            checked={prefs.notifyOnMention ?? true}
            onChange={v => onUpdate({ notifyOnMention: v })}
            disabled={isUpdating}
          />
        </div>
      </div>
    </div>
  );
}

// =============================================================================
// Main Page
// =============================================================================

export default function PreferencesPage() {
  const { data, isLoading, error } = usePreferences();
  const updatePrefs = useUpdatePreferences();
  const [localPrefs, setLocalPrefs] = useState<UserPreferences>({});

  // Sync remote preferences to local state
  useEffect(() => {
    if (data?.preferences) {
      setLocalPrefs(data.preferences);
    }
  }, [data]);

  const handleUpdate = async (updates: Partial<UserPreferences>) => {
    // Optimistic update
    setLocalPrefs(prev => ({ ...prev, ...updates }));

    try {
      await updatePrefs.mutateAsync(updates);
      toast.success('Preferences saved');
    } catch {
      // Revert on error
      if (data?.preferences) {
        setLocalPrefs(data.preferences);
      }
      toast.error('Failed to save preferences');
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-6">
        <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
          <div className="flex items-center gap-3 mb-4">
            <Settings width={20} height={20} className="text-sc-purple" />
            <h2 className="text-lg font-semibold text-sc-fg-primary">Preferences</h2>
          </div>
          <SectionSkeleton />
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-6">
        <div className="bg-sc-bg-base rounded-lg border border-sc-red/20 p-6">
          <p className="text-sc-red">Failed to load preferences. Please try again.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="bg-sc-bg-base rounded-lg border border-sc-fg-subtle/10 p-6">
        <div className="flex items-center gap-3 mb-4">
          <Settings width={20} height={20} className="text-sc-purple" />
          <h2 className="text-lg font-semibold text-sc-fg-primary">Preferences</h2>
        </div>
        <p className="text-sc-fg-muted">
          Customize your display preferences, language, and behavior settings.
        </p>
      </div>

      <AppearanceSection
        prefs={localPrefs}
        onUpdate={handleUpdate}
        isUpdating={updatePrefs.isPending}
      />
      <LocaleSection
        prefs={localPrefs}
        onUpdate={handleUpdate}
        isUpdating={updatePrefs.isPending}
      />
      <GraphSection prefs={localPrefs} onUpdate={handleUpdate} isUpdating={updatePrefs.isPending} />
      <NotificationsSection
        prefs={localPrefs}
        onUpdate={handleUpdate}
        isUpdating={updatePrefs.isPending}
      />
    </div>
  );
}
