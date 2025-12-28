import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { Button } from '../button';
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '../dialog';
import { Input } from '../input';

const meta = {
  title: 'UI/Dialog',
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
} satisfies Meta;

export default meta;

export const Default: StoryObj = {
  render: () => (
    <Dialog>
      <DialogTrigger asChild>
        <Button>Open Dialog</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Dialog Title</DialogTitle>
          <DialogDescription>
            This is a description of what this dialog does. It provides context for the user.
          </DialogDescription>
        </DialogHeader>
        <div className="py-4">
          <p className="text-sc-fg-primary">Dialog content goes here.</p>
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Cancel</Button>
          </DialogClose>
          <Button>Continue</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  ),
};

export const WithForm: StoryObj = {
  render: () => (
    <Dialog>
      <DialogTrigger asChild>
        <Button>Create Project</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Create New Project</DialogTitle>
          <DialogDescription>
            Add a new project to organize your tasks and knowledge.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 py-4">
          <div>
            <label
              htmlFor="project-name"
              className="text-sm font-medium text-sc-fg-muted block mb-2"
            >
              Project Name
            </label>
            <Input id="project-name" placeholder="My Awesome Project" />
          </div>
          <div>
            <label
              htmlFor="project-desc"
              className="text-sm font-medium text-sc-fg-muted block mb-2"
            >
              Description
            </label>
            <Input id="project-desc" placeholder="Optional description..." />
          </div>
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Cancel</Button>
          </DialogClose>
          <Button>Create Project</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  ),
};

export const SmallSize: StoryObj = {
  render: () => (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="secondary">Small Dialog</Button>
      </DialogTrigger>
      <DialogContent size="sm">
        <DialogHeader>
          <DialogTitle>Confirm Action</DialogTitle>
          <DialogDescription>Are you sure you want to proceed?</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">No</Button>
          </DialogClose>
          <Button>Yes</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  ),
};

export const LargeSize: StoryObj = {
  render: () => (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="outline">Large Dialog</Button>
      </DialogTrigger>
      <DialogContent size="lg">
        <DialogHeader>
          <DialogTitle>Entity Details</DialogTitle>
          <DialogDescription>View and edit the complete entity information.</DialogDescription>
        </DialogHeader>
        <div className="py-4 space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div className="bg-sc-bg-highlight p-4 rounded-lg">
              <p className="text-sm text-sc-fg-muted">Type</p>
              <p className="text-sc-fg-primary font-medium">Pattern</p>
            </div>
            <div className="bg-sc-bg-highlight p-4 rounded-lg">
              <p className="text-sm text-sc-fg-muted">Created</p>
              <p className="text-sc-fg-primary font-medium">2 days ago</p>
            </div>
          </div>
          <div className="bg-sc-bg-highlight p-4 rounded-lg">
            <p className="text-sm text-sc-fg-muted mb-2">Content</p>
            <p className="text-sc-fg-primary">
              This is the full content of the entity. It can be quite long and contain detailed
              information about the pattern, rule, or other knowledge type.
            </p>
          </div>
        </div>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Close</Button>
          </DialogClose>
          <Button variant="secondary">Edit</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  ),
};

export const DangerAction: StoryObj = {
  render: () => (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="danger">Delete Item</Button>
      </DialogTrigger>
      <DialogContent size="sm">
        <DialogHeader>
          <DialogTitle>Delete Confirmation</DialogTitle>
          <DialogDescription>
            This action cannot be undone. This will permanently delete the item.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <DialogClose asChild>
            <Button variant="ghost">Cancel</Button>
          </DialogClose>
          <Button variant="danger">Delete</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  ),
};

export const NoCloseButton: StoryObj = {
  render: () => (
    <Dialog>
      <DialogTrigger asChild>
        <Button variant="ghost">Open (No X)</Button>
      </DialogTrigger>
      <DialogContent showClose={false}>
        <DialogHeader>
          <DialogTitle>Required Action</DialogTitle>
          <DialogDescription>You must complete this action to continue.</DialogDescription>
        </DialogHeader>
        <div className="py-4">
          <Input placeholder="Enter required information..." />
        </div>
        <DialogFooter>
          <Button className="w-full">Submit</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  ),
};
