import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { Search } from '../icons';
import { Input, Label, SearchInput, Textarea } from '../input';

const meta = {
  title: 'UI/Input',
  component: Input,
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
  argTypes: {
    placeholder: { control: 'text' },
    disabled: { control: 'boolean' },
    error: { control: 'text' },
  },
} satisfies Meta<typeof Input>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Default: Story = {
  args: {
    placeholder: 'Enter text...',
  },
};

export const WithIcon: Story = {
  args: {
    placeholder: 'Search...',
    icon: <Search className="w-4 h-4" />,
  },
};

export const WithError: Story = {
  args: {
    placeholder: 'Email address',
    defaultValue: 'invalid-email',
    error: 'Please enter a valid email address',
  },
};

export const Disabled: Story = {
  args: {
    placeholder: 'Disabled input',
    disabled: true,
    defaultValue: 'Cannot edit this',
  },
};

export const SearchInputVariant: StoryObj = {
  render: () => (
    <div className="w-[400px]">
      <SearchInput placeholder="Search the knowledge graph..." />
    </div>
  ),
};

export const TextareaDefault: StoryObj = {
  render: () => (
    <div className="w-[400px]">
      <Textarea placeholder="Enter a longer description..." rows={4} />
    </div>
  ),
};

export const TextareaMonospace: StoryObj = {
  render: () => (
    <div className="w-[400px]">
      <Textarea placeholder="Enter code or technical content..." rows={4} monospace />
    </div>
  ),
};

export const TextareaWithError: StoryObj = {
  render: () => (
    <div className="w-[400px]">
      <Textarea placeholder="Required field" error="This field is required" rows={3} />
    </div>
  ),
};

export const LabelExamples: StoryObj = {
  render: () => (
    <div className="space-y-4 w-[300px]">
      <div>
        <Label>Basic Label</Label>
        <Input placeholder="Basic input" />
      </div>
      <div>
        <Label required>Required Field</Label>
        <Input placeholder="This is required" />
      </div>
      <div>
        <Label description="This will be shown to other users">Display Name</Label>
        <Input placeholder="Your name" />
      </div>
    </div>
  ),
};

export const AllInputTypes: StoryObj = {
  render: () => (
    <div className="space-y-4 w-[400px]">
      <div>
        <Label>Text Input</Label>
        <Input placeholder="Regular text" />
      </div>
      <div>
        <Label>With Icon</Label>
        <Input placeholder="Search..." icon={<Search className="w-4 h-4" />} />
      </div>
      <div>
        <Label>With Error</Label>
        <Input placeholder="Email" error="Invalid email" />
      </div>
      <div>
        <Label>Search Input (Large)</Label>
        <SearchInput placeholder="Search everything..." />
      </div>
      <div>
        <Label>Textarea</Label>
        <Textarea placeholder="Multi-line input..." rows={3} />
      </div>
    </div>
  ),
};
