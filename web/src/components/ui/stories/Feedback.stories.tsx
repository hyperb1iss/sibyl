import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { Button } from '../button';
import { EmptyState, ErrorState, Hint, InfoTooltip, SuccessState, Tooltip } from '../tooltip';

const meta = {
  title: 'UI/Feedback',
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
} satisfies Meta;

export default meta;

// Tooltip Stories
export const TooltipBasic: StoryObj = {
  render: () => (
    <div className="flex gap-8">
      <Tooltip content="This is a tooltip" side="top">
        <Button variant="secondary">Hover me (top)</Button>
      </Tooltip>
      <Tooltip content="Tooltip on bottom" side="bottom">
        <Button variant="secondary">Hover me (bottom)</Button>
      </Tooltip>
      <Tooltip content="Left tooltip" side="left">
        <Button variant="secondary">Hover me (left)</Button>
      </Tooltip>
      <Tooltip content="Right tooltip" side="right">
        <Button variant="secondary">Hover me (right)</Button>
      </Tooltip>
    </div>
  ),
};

export const TooltipWithDelay: StoryObj = {
  render: () => (
    <div className="flex gap-8">
      <Tooltip content="Instant tooltip" delay={0}>
        <Button variant="ghost">No delay</Button>
      </Tooltip>
      <Tooltip content="Slow tooltip" delay={500}>
        <Button variant="ghost">500ms delay</Button>
      </Tooltip>
    </div>
  ),
};

export const InfoTooltipSizes: StoryObj = {
  render: () => (
    <div className="flex items-center gap-8">
      <div className="flex items-center gap-2">
        <span className="text-sc-fg-primary">Small</span>
        <InfoTooltip content="This is helpful information" size="sm" />
      </div>
      <div className="flex items-center gap-2">
        <span className="text-sc-fg-primary">Medium</span>
        <InfoTooltip content="This is helpful information" size="md" />
      </div>
    </div>
  ),
};

// Empty State Stories
export const EmptyStateDefault: StoryObj = {
  render: () => (
    <div className="w-[500px]">
      <EmptyState
        title="No entities found"
        description="Start by adding your first pattern or rule to the knowledge graph."
        action={<Button>Add Entity</Button>}
      />
    </div>
  ),
};

export const EmptyStateVariants: StoryObj = {
  render: () => (
    <div className="grid grid-cols-2 gap-8 w-[800px]">
      <div className="border border-sc-fg-subtle/20 rounded-xl p-4">
        <EmptyState
          variant="search"
          title="No results found"
          description="Try adjusting your search query"
        />
      </div>
      <div className="border border-sc-fg-subtle/20 rounded-xl p-4">
        <EmptyState
          variant="data"
          title="No data yet"
          description="Data will appear here once available"
        />
      </div>
      <div className="border border-sc-fg-subtle/20 rounded-xl p-4">
        <EmptyState
          variant="create"
          title="Get started"
          description="Create your first item"
          action={<Button>Create</Button>}
        />
      </div>
      <div className="border border-sc-fg-subtle/20 rounded-xl p-4">
        <EmptyState variant="default" title="Nothing here" description="This section is empty" />
      </div>
    </div>
  ),
};

// Error State Stories
export const ErrorStateVariants: StoryObj = {
  render: () => (
    <div className="space-y-8 w-[500px]">
      <div className="border border-sc-fg-subtle/20 rounded-xl p-4">
        <ErrorState
          variant="error"
          message="Failed to load entities. Please try again."
          action={<Button variant="secondary">Retry</Button>}
        />
      </div>
      <div className="border border-sc-fg-subtle/20 rounded-xl p-4">
        <ErrorState
          variant="warning"
          title="Rate limited"
          message="You've made too many requests. Please wait a moment."
        />
      </div>
      <div className="border border-sc-fg-subtle/20 rounded-xl p-4">
        <ErrorState
          variant="offline"
          message="Check your internet connection and try again."
          action={<Button variant="ghost">Refresh</Button>}
        />
      </div>
    </div>
  ),
};

// Success State Stories
export const SuccessStateExamples: StoryObj = {
  render: () => (
    <div className="space-y-8 w-[500px]">
      <div className="border border-sc-fg-subtle/20 rounded-xl p-4">
        <SuccessState
          title="Entity created!"
          message="Your new pattern has been added to the knowledge graph."
          action={<Button>View Entity</Button>}
        />
      </div>
      <div className="border border-sc-fg-subtle/20 rounded-xl p-4">
        <SuccessState title="Task completed!" celebratory={false} />
      </div>
    </div>
  ),
};

// Hint Stories
export const HintVariants: StoryObj = {
  render: () => (
    <div className="space-y-4 w-[500px]">
      <Hint variant="info">
        <strong>Pro tip:</strong> Use semantic search to find related patterns across the knowledge
        graph.
      </Hint>
      <Hint variant="tip">
        <strong>Did you know?</strong> You can link entities together to build a richer knowledge
        network.
      </Hint>
      <Hint variant="warning">
        <strong>Heads up:</strong> This action will affect all linked entities.
      </Hint>
    </div>
  ),
};

export const HintDismissible: StoryObj = {
  render: () => (
    <div className="w-[500px]">
      <Hint variant="tip" dismissible onDismiss={() => console.log('Dismissed!')}>
        This is a dismissible hint. Click the X to close it.
      </Hint>
    </div>
  ),
};
