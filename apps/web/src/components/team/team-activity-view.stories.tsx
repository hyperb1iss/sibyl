import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { useLayoutEffect, useState } from 'react';
import type { TeamActivityResponse, TeamActivityWindow } from '@/lib/api/activity';
import { FIXTURE_PROJECTS, fixtureQuietTeam, fixtureTeamActivity } from './team-activity.fixtures';
import { TeamActivityView } from './team-activity-view';

const NOW = Date.parse('2026-10-08T17:30:00');
const PROJECT_NAMES = Object.fromEntries(
  FIXTURE_PROJECTS.map(project => [project.id, project.name])
);

type Scenario = 'active' | 'truncated' | 'quiet' | 'loading' | 'error';

function responseFor(scenario: Scenario, window: TeamActivityWindow) {
  if (scenario === 'quiet') return fixtureQuietTeam(NOW, window);
  if (scenario === 'loading' || scenario === 'error') return undefined;
  return fixtureTeamActivity(NOW, window, { truncated: scenario === 'truncated' });
}

interface PlaygroundProps {
  scenario: Scenario;
  theme: 'neon' | 'dawn';
}

/** The view with local state standing in for the URL and project context. */
function TeamActivityPlayground({ scenario, theme }: PlaygroundProps) {
  const [activityWindow, setActivityWindow] = useState<TeamActivityWindow>('7d');
  const [selected, setSelected] = useState<string[]>([]);

  useLayoutEffect(() => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme === 'dawn' ? 'light' : 'dark';
  }, [theme]);

  const data: TeamActivityResponse | undefined = responseFor(scenario, activityWindow);

  return (
    <div className="min-h-screen bg-sc-bg-dark p-3 font-sans sm:p-4 md:p-6">
      <TeamActivityView
        activityWindow={activityWindow}
        onWindowChange={setActivityWindow}
        projects={FIXTURE_PROJECTS}
        projectNames={PROJECT_NAMES}
        selectedProjectIds={selected}
        onSelectAllProjects={() => setSelected([])}
        onSelectProject={id => setSelected([id])}
        activity={{
          data,
          isLoading: scenario === 'loading',
          isFetching: scenario === 'loading',
          isPlaceholderData: false,
          isError: scenario === 'error',
          refetch: () => undefined,
        }}
        currentUserId="user_grace"
        now={NOW}
      />
    </div>
  );
}

const meta = {
  title: 'Team/Team Activity',
  component: TeamActivityPlayground,
  parameters: { layout: 'fullscreen' },
  args: { scenario: 'active', theme: 'neon' },
  argTypes: {
    scenario: {
      control: 'select',
      options: ['active', 'truncated', 'quiet', 'loading', 'error'],
    },
    theme: { control: 'inline-radio', options: ['neon', 'dawn'] },
  },
} satisfies Meta<typeof TeamActivityPlayground>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Neon: Story = {};

export const Dawn: Story = { args: { theme: 'dawn' } };

export const Truncated: Story = { args: { scenario: 'truncated' } };

export const QuietTeam: Story = { args: { scenario: 'quiet' } };

export const Loading: Story = { args: { scenario: 'loading' } };

export const LoadFailed: Story = { args: { scenario: 'error' } };
