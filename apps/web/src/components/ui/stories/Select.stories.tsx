import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { useState } from 'react';
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectSeparator,
  SelectTrigger,
  SelectValue,
} from '../select';

const meta = {
  title: 'UI/Select',
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
} satisfies Meta;

export default meta;

export const Default: StoryObj = {
  render: () => (
    <Select>
      <SelectTrigger className="w-[200px]">
        <SelectValue placeholder="Select an option" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="option1">Option 1</SelectItem>
        <SelectItem value="option2">Option 2</SelectItem>
        <SelectItem value="option3">Option 3</SelectItem>
      </SelectContent>
    </Select>
  ),
};

export const WithGroups: StoryObj = {
  render: () => (
    <Select>
      <SelectTrigger className="w-[220px]">
        <SelectValue placeholder="Select entity type" />
      </SelectTrigger>
      <SelectContent>
        <SelectGroup>
          <SelectLabel>Knowledge</SelectLabel>
          <SelectItem value="pattern">Pattern</SelectItem>
          <SelectItem value="rule">Rule</SelectItem>
          <SelectItem value="episode">Episode</SelectItem>
        </SelectGroup>
        <SelectSeparator />
        <SelectGroup>
          <SelectLabel>Work</SelectLabel>
          <SelectItem value="task">Task</SelectItem>
          <SelectItem value="project">Project</SelectItem>
        </SelectGroup>
        <SelectSeparator />
        <SelectGroup>
          <SelectLabel>Content</SelectLabel>
          <SelectItem value="source">Source</SelectItem>
          <SelectItem value="document">Document</SelectItem>
        </SelectGroup>
      </SelectContent>
    </Select>
  ),
};

export const WithDefaultValue: StoryObj = {
  render: () => (
    <Select defaultValue="medium">
      <SelectTrigger className="w-[180px]">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="low">Low Priority</SelectItem>
        <SelectItem value="medium">Medium Priority</SelectItem>
        <SelectItem value="high">High Priority</SelectItem>
        <SelectItem value="critical">Critical Priority</SelectItem>
      </SelectContent>
    </Select>
  ),
};

export const Disabled: StoryObj = {
  render: () => (
    <Select disabled>
      <SelectTrigger className="w-[200px]">
        <SelectValue placeholder="Disabled select" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="option1">Option 1</SelectItem>
      </SelectContent>
    </Select>
  ),
};

export const DisabledItems: StoryObj = {
  render: () => (
    <Select>
      <SelectTrigger className="w-[200px]">
        <SelectValue placeholder="Some options disabled" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="available1">Available</SelectItem>
        <SelectItem value="disabled1" disabled>
          Disabled Option
        </SelectItem>
        <SelectItem value="available2">Also Available</SelectItem>
        <SelectItem value="disabled2" disabled>
          Also Disabled
        </SelectItem>
      </SelectContent>
    </Select>
  ),
};

export const Controlled: StoryObj = {
  render: function ControlledSelect() {
    const [value, setValue] = useState('');

    return (
      <div className="space-y-4">
        <Select value={value} onValueChange={setValue}>
          <SelectTrigger className="w-[200px]">
            <SelectValue placeholder="Choose status" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="todo">To Do</SelectItem>
            <SelectItem value="doing">In Progress</SelectItem>
            <SelectItem value="review">In Review</SelectItem>
            <SelectItem value="done">Done</SelectItem>
          </SelectContent>
        </Select>
        <p className="text-sm text-sc-fg-muted">
          Selected: <span className="text-sc-cyan">{value || 'none'}</span>
        </p>
      </div>
    );
  },
};

export const FullWidth: StoryObj = {
  render: () => (
    <div className="w-[400px]">
      <Select>
        <SelectTrigger>
          <SelectValue placeholder="Full width select" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value="option1">First Option</SelectItem>
          <SelectItem value="option2">Second Option</SelectItem>
          <SelectItem value="option3">Third Option</SelectItem>
        </SelectContent>
      </Select>
    </div>
  ),
};
