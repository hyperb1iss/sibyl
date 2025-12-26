import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { useState } from 'react';
import { BadgeList, EntityBadge, RemovableBadge, StatusBadge } from '../badge';

const meta = {
  title: 'UI/Badge',
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
} satisfies Meta;

export default meta;

export const EntityBadges: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-3">
      <EntityBadge type="pattern" />
      <EntityBadge type="rule" />
      <EntityBadge type="episode" />
      <EntityBadge type="task" />
      <EntityBadge type="project" />
      <EntityBadge type="topic" />
      <EntityBadge type="source" />
      <EntityBadge type="document" />
    </div>
  ),
};

export const EntityBadgesWithIcons: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-3">
      <EntityBadge type="pattern" showIcon />
      <EntityBadge type="rule" showIcon />
      <EntityBadge type="task" showIcon />
      <EntityBadge type="project" showIcon />
    </div>
  ),
};

export const EntityBadgeSizes: StoryObj = {
  render: () => (
    <div className="flex items-center gap-3">
      <EntityBadge type="pattern" size="sm" showIcon />
      <EntityBadge type="pattern" size="md" showIcon />
      <EntityBadge type="pattern" size="lg" showIcon />
    </div>
  ),
};

export const StatusBadges: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-3">
      <StatusBadge status="healthy" />
      <StatusBadge status="unhealthy" />
      <StatusBadge status="warning" />
      <StatusBadge status="idle" />
      <StatusBadge status="running" />
    </div>
  ),
};

export const StatusBadgesWithPulse: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-3">
      <StatusBadge status="healthy" pulse />
      <StatusBadge status="running" pulse label="Processing" />
    </div>
  ),
};

export const StatusBadgesCustomLabels: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-3">
      <StatusBadge status="healthy" label="Connected" />
      <StatusBadge status="unhealthy" label="Disconnected" />
      <StatusBadge status="warning" label="Rate Limited" />
    </div>
  ),
};

export const RemovableBadgeColors: StoryObj = {
  render: () => (
    <div className="flex flex-wrap gap-3">
      <RemovableBadge color="purple" onRemove={() => undefined}>
        Purple
      </RemovableBadge>
      <RemovableBadge color="cyan" onRemove={() => undefined}>
        Cyan
      </RemovableBadge>
      <RemovableBadge color="coral" onRemove={() => undefined}>
        Coral
      </RemovableBadge>
      <RemovableBadge color="yellow" onRemove={() => undefined}>
        Yellow
      </RemovableBadge>
      <RemovableBadge color="green" onRemove={() => undefined}>
        Green
      </RemovableBadge>
      <RemovableBadge color="red" onRemove={() => undefined}>
        Red
      </RemovableBadge>
      <RemovableBadge color="gray" onRemove={() => undefined}>
        Gray
      </RemovableBadge>
    </div>
  ),
};

export const RemovableBadgeSizes: StoryObj = {
  render: () => (
    <div className="flex items-center gap-3">
      <RemovableBadge size="sm" color="cyan" onRemove={() => undefined}>
        Small
      </RemovableBadge>
      <RemovableBadge size="md" color="cyan" onRemove={() => undefined}>
        Medium
      </RemovableBadge>
      <RemovableBadge size="lg" color="cyan" onRemove={() => undefined}>
        Large
      </RemovableBadge>
    </div>
  ),
};

export const RemovableBadgeInteractive: StoryObj = {
  render: function InteractiveBadges() {
    const [tags, setTags] = useState(['typescript', 'react', 'nextjs', 'storybook']);

    return (
      <div className="space-y-4">
        <p className="text-sc-fg-muted text-sm">Click X to remove tags:</p>
        <BadgeList>
          {tags.map(tag => (
            <RemovableBadge
              key={tag}
              color="purple"
              onRemove={() => setTags(t => t.filter(x => x !== tag))}
            >
              {tag}
            </RemovableBadge>
          ))}
        </BadgeList>
        {tags.length === 0 && (
          <button
            type="button"
            onClick={() => setTags(['typescript', 'react', 'nextjs', 'storybook'])}
            className="text-sc-cyan text-sm hover:underline"
          >
            Reset tags
          </button>
        )}
      </div>
    );
  },
};

export const DisabledRemovableBadge: StoryObj = {
  render: () => (
    <RemovableBadge color="gray" onRemove={() => undefined} disabled>
      Disabled
    </RemovableBadge>
  ),
};
