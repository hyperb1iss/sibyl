'use client';

import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { SettingsPageHeader, SettingsRow, SettingsSection } from '@/components/settings/primitives';
import { Eye, Flash, Globe, Network, Settings } from '@/components/ui/icons';
import type { UserPreferences } from '@/lib/api';
import { usePreferences, useUpdatePreferences } from '@/lib/hooks';
import { type ThemePreference, useTheme } from '@/lib/theme';

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

// Map between display values and internal theme values
const toDisplayValue = (pref: ThemePreference): string => {
  if (pref === 'neon') return 'dark';
  if (pref === 'dawn') return 'light';
  return 'system';
};

const toThemePreference = (display: string): ThemePreference => {
  if (display === 'dark') return 'neon';
  if (display === 'light') return 'dawn';
  return 'system';
};

interface AppearanceSectionProps {
  backendTheme?: 'light' | 'dark' | 'system';
  onBackendUpdate: (theme: 'light' | 'dark' | 'system') => void;
  isUpdating: boolean;
}

function AppearanceSection({ backendTheme, onBackendUpdate, isUpdating }: AppearanceSectionProps) {
  const { preference, setPreference } = useTheme();

  // Sync backend theme to localStorage on mount (backend is source of truth)
  // Only runs when backendTheme changes - intentionally excludes preference/setPreference to avoid loops
  // biome-ignore lint/correctness/useExhaustiveDependencies: sync only on backend change
  useEffect(() => {
    if (backendTheme) {
      const localPref = toThemePreference(backendTheme);
      if (localPref !== preference) {
        setPreference(localPref);
      }
    }
  }, [backendTheme]);

  const handleThemeChange = (displayValue: string) => {
    const themePref = toThemePreference(displayValue);
    // Update localStorage (immediate)
    setPreference(themePref);
    // Update backend (persisted)
    onBackendUpdate(displayValue as 'light' | 'dark' | 'system');
  };

  const themes: SelectOption[] = [
    { value: 'system', label: 'System Default' },
    { value: 'dark', label: 'Dark (Neon)' },
    { value: 'light', label: 'Light (Dawn)' },
  ];

  return (
    <SettingsSection title="Appearance" icon={Eye} iconColor="text-sc-purple">
      <SettingsRow
        label="Theme"
        description="Choose your preferred color theme"
        control={
          <Select
            value={toDisplayValue(preference)}
            onChange={handleThemeChange}
            options={themes}
            disabled={isUpdating}
          />
        }
      />
    </SettingsSection>
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
    <SettingsSection title="Language & Region" icon={Globe} iconColor="text-sc-cyan">
      <SettingsRow
        label="Language"
        description="Select your preferred language"
        divider
        control={
          <Select
            value={prefs.locale || 'en'}
            onChange={v => onUpdate({ locale: v })}
            options={locales}
            disabled={isUpdating}
          />
        }
      />
      <SettingsRow
        label="Timezone"
        description="Used for displaying dates and times"
        control={
          <Select
            value={prefs.timezone || 'auto'}
            onChange={v => onUpdate({ timezone: v })}
            options={timezones}
            disabled={isUpdating}
          />
        }
      />
    </SettingsSection>
  );
}

// =============================================================================
// Graph Section
// =============================================================================

function GraphSection({ prefs, onUpdate, isUpdating }: SectionProps) {
  return (
    <SettingsSection title="Knowledge Graph" icon={Network} iconColor="text-sc-coral">
      <SettingsRow
        label="Show Labels"
        description="Display labels on graph nodes by default"
        divider
        control={
          <Toggle
            checked={prefs.graphShowLabels ?? true}
            onChange={v => onUpdate({ graphShowLabels: v })}
            disabled={isUpdating}
          />
        }
      />
      <SettingsRow
        label="Default Zoom Level"
        description="Initial zoom when opening the graph"
        divider
        control={
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
        }
      />
      <SettingsRow
        label="Default Dashboard View"
        description="Layout for dashboard and entity lists"
        control={
          <Select
            value={prefs.dashboardDefaultView || 'grid'}
            onChange={v => onUpdate({ dashboardDefaultView: v as 'grid' | 'list' })}
            options={[
              { value: 'grid', label: 'Grid' },
              { value: 'list', label: 'List' },
            ]}
            disabled={isUpdating}
          />
        }
      />
    </SettingsSection>
  );
}

// =============================================================================
// Notifications Section
// =============================================================================

function NotificationsSection({ prefs, onUpdate, isUpdating }: SectionProps) {
  return (
    <SettingsSection title="Notifications" icon={Flash} iconColor="text-sc-yellow">
      <SettingsRow
        label="Task Assignments"
        description="Notify when tasks are assigned to you"
        divider
        control={
          <Toggle
            checked={prefs.notifyOnTaskAssigned ?? true}
            onChange={v => onUpdate({ notifyOnTaskAssigned: v })}
            disabled={isUpdating}
          />
        }
      />
      <SettingsRow
        label="Mentions"
        description="Notify when you are mentioned"
        control={
          <Toggle
            checked={prefs.notifyOnMention ?? true}
            onChange={v => onUpdate({ notifyOnMention: v })}
            disabled={isUpdating}
          />
        }
      />
    </SettingsSection>
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
        <SettingsPageHeader
          icon={Settings}
          title="Preferences"
          description="Customize your display, language, and behavior."
        />
        <SectionSkeleton />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-6">
        <SettingsPageHeader
          icon={Settings}
          title="Preferences"
          description="Customize your display, language, and behavior."
        />
        <div className="rounded-lg border border-sc-red/20 bg-sc-red/5 p-4 text-sm text-sc-red">
          Failed to load preferences. Please try again.
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <SettingsPageHeader
        icon={Settings}
        title="Preferences"
        description="Customize your display, language, and behavior."
      />
      <AppearanceSection
        backendTheme={localPrefs.theme}
        onBackendUpdate={theme => handleUpdate({ theme })}
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
