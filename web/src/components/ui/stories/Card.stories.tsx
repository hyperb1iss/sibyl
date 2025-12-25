import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { Card, CardHeader, CollapsibleCard, StatCard } from '../card';
import { AlertTriangle, Database, Sparkles } from '../icons';

const meta = {
  title: 'UI/Card',
  component: Card,
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
  argTypes: {
    variant: {
      control: 'select',
      options: ['default', 'elevated', 'interactive', 'bordered', 'error', 'warning', 'success'],
    },
    glow: { control: 'boolean' },
    gradientBorder: { control: 'boolean' },
  },
} satisfies Meta<typeof Card>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {
  args: {
    children: <p className="text-sc-fg-muted">This is a default card with some content.</p>,
    variant: 'default',
  },
};

export const Elevated: Story = {
  args: {
    children: <p className="text-sc-fg-muted">Elevated card with shadow.</p>,
    variant: 'elevated',
  },
};

export const Interactive: Story = {
  args: {
    children: <p className="text-sc-fg-muted">Hover me! I'm interactive.</p>,
    variant: 'interactive',
  },
};

export const ErrorState: Story = {
  args: {
    children: (
      <div className="flex items-center gap-3">
        <AlertTriangle className="text-sc-red" />
        <p className="text-sc-fg-muted">Something went wrong.</p>
      </div>
    ),
    variant: 'error',
  },
};

export const Warning: Story = {
  args: {
    children: <p className="text-sc-fg-muted">This is a warning card.</p>,
    variant: 'warning',
  },
};

export const Success: Story = {
  args: {
    children: <p className="text-sc-fg-muted">Operation completed successfully!</p>,
    variant: 'success',
  },
};

export const WithGlow: Story = {
  args: {
    children: <p className="text-sc-fg-muted">This card has a glowing effect.</p>,
    variant: 'default',
    glow: true,
  },
};

export const WithHeader: Story = {
  args: {
    children: <p className="text-sc-fg-muted">Card content goes here.</p>,
    className: 'w-80',
  },
  render: args => (
    <Card {...args}>
      <CardHeader
        title="Card Title"
        description="This is a description"
        icon={<Database className="w-5 h-5" />}
      />
      {args.children}
    </Card>
  ),
};

export const StatCardExample: Story = {
  args: {
    children: null,
  },
  render: () => (
    <div className="grid grid-cols-2 gap-4">
      <StatCard label="Total Users" value="1,234" trend="up" sublabel="+12% from last month" />
      <StatCard
        label="Active Sessions"
        value="42"
        icon={<Sparkles className="w-5 h-5" />}
        trend="neutral"
        sublabel="No change"
      />
    </div>
  ),
};

export const CollapsibleExample: Story = {
  args: {
    children: null,
  },
  render: () => (
    <CollapsibleCard
      title="Expandable Section"
      description="Click to expand"
      icon={<Database className="w-5 h-5" />}
      defaultOpen
    >
      <p className="text-sc-fg-muted">
        This content is hidden by default and can be expanded by clicking the header.
      </p>
    </CollapsibleCard>
  ),
};

export const AllVariants: Story = {
  args: {
    children: 'Card',
  },
  render: () => (
    <div className="grid grid-cols-2 gap-4 max-w-2xl">
      <Card variant="default">Default</Card>
      <Card variant="elevated">Elevated</Card>
      <Card variant="interactive">Interactive</Card>
      <Card variant="bordered">Bordered</Card>
      <Card variant="error">Error</Card>
      <Card variant="warning">Warning</Card>
      <Card variant="success">Success</Card>
    </div>
  ),
};
