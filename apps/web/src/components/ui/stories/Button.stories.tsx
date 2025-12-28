import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { Button } from '../button';
import { Plus } from '../icons';

const meta = {
  title: 'UI/Button',
  component: Button,
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
  argTypes: {
    variant: {
      control: 'select',
      options: ['primary', 'secondary', 'ghost', 'danger', 'outline', 'link'],
    },
    size: {
      control: 'select',
      options: ['sm', 'md', 'lg'],
    },
    loading: { control: 'boolean' },
    disabled: { control: 'boolean' },
  },
} satisfies Meta<typeof Button>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Primary: Story = {
  args: {
    children: 'Primary Button',
    variant: 'primary',
  },
};

export const Secondary: Story = {
  args: {
    children: 'Secondary Button',
    variant: 'secondary',
  },
};

export const Ghost: Story = {
  args: {
    children: 'Ghost Button',
    variant: 'ghost',
  },
};

export const Danger: Story = {
  args: {
    children: 'Danger Button',
    variant: 'danger',
  },
};

export const Outline: Story = {
  args: {
    children: 'Outline Button',
    variant: 'outline',
  },
};

export const Link: Story = {
  args: {
    children: 'Link Button',
    variant: 'link',
  },
};

export const WithIcon: Story = {
  args: {
    children: 'Add Item',
    icon: <Plus className="w-4 h-4" />,
  },
};

export const Loading: Story = {
  args: {
    children: 'Loading...',
    loading: true,
  },
};

export const Disabled: Story = {
  args: {
    children: 'Disabled',
    disabled: true,
  },
};

export const AllSizes: Story = {
  args: {
    children: 'Button',
  },
  render: args => (
    <div className="flex items-center gap-4">
      <Button {...args} size="sm">
        Small
      </Button>
      <Button {...args} size="md">
        Medium
      </Button>
      <Button {...args} size="lg">
        Large
      </Button>
    </div>
  ),
};

export const AllVariants: Story = {
  args: {
    children: 'Button',
  },
  render: args => (
    <div className="flex flex-wrap items-center gap-4">
      <Button {...args} variant="primary">
        Primary
      </Button>
      <Button {...args} variant="secondary">
        Secondary
      </Button>
      <Button {...args} variant="ghost">
        Ghost
      </Button>
      <Button {...args} variant="danger">
        Danger
      </Button>
      <Button {...args} variant="outline">
        Outline
      </Button>
      <Button {...args} variant="link">
        Link
      </Button>
    </div>
  ),
};
