import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { useState } from 'react';
import {
  LoadingState,
  ProgressSpinner,
  Skeleton,
  SkeletonCard,
  SkeletonList,
  Spinner,
} from '../spinner';

const meta = {
  title: 'UI/Spinner',
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
} satisfies Meta;

export default meta;

export const SpinnerSizes: StoryObj = {
  render: () => (
    <div className="flex items-center gap-6">
      <div className="text-center">
        <Spinner size="sm" />
        <p className="text-xs text-sc-fg-muted mt-2">Small</p>
      </div>
      <div className="text-center">
        <Spinner size="md" />
        <p className="text-xs text-sc-fg-muted mt-2">Medium</p>
      </div>
      <div className="text-center">
        <Spinner size="lg" />
        <p className="text-xs text-sc-fg-muted mt-2">Large</p>
      </div>
      <div className="text-center">
        <Spinner size="xl" />
        <p className="text-xs text-sc-fg-muted mt-2">XL</p>
      </div>
    </div>
  ),
};

export const SpinnerColors: StoryObj = {
  render: () => (
    <div className="flex items-center gap-6">
      <div className="text-center">
        <Spinner color="purple" />
        <p className="text-xs text-sc-fg-muted mt-2">Purple</p>
      </div>
      <div className="text-center">
        <Spinner color="cyan" />
        <p className="text-xs text-sc-fg-muted mt-2">Cyan</p>
      </div>
      <div className="text-center bg-sc-purple p-4 rounded-lg">
        <Spinner color="white" />
        <p className="text-xs text-white mt-2">White</p>
      </div>
      <div className="text-center text-sc-coral">
        <Spinner color="current" />
        <p className="text-xs mt-2">Current</p>
      </div>
    </div>
  ),
};

export const SpinnerVariants: StoryObj = {
  render: () => (
    <div className="flex items-center gap-12">
      <div className="text-center">
        <Spinner variant="default" size="lg" />
        <p className="text-xs text-sc-fg-muted mt-4">Default</p>
      </div>
      <div className="text-center">
        <Spinner variant="orbital" size="lg" />
        <p className="text-xs text-sc-fg-muted mt-4">Orbital</p>
      </div>
      <div className="text-center">
        <Spinner variant="gradient" size="lg" />
        <p className="text-xs text-sc-fg-muted mt-4">Gradient</p>
      </div>
    </div>
  ),
};

export const LoadingStateDefault: StoryObj = {
  render: () => (
    <div className="w-[400px] border border-sc-fg-subtle/20 rounded-xl">
      <LoadingState message="Loading entities..." />
    </div>
  ),
};

export const LoadingStatePlayful: StoryObj = {
  render: () => (
    <div className="w-[400px] border border-sc-fg-subtle/20 rounded-xl">
      <LoadingState playful />
    </div>
  ),
};

export const LoadingStateVariants: StoryObj = {
  render: () => (
    <div className="grid grid-cols-3 gap-4 w-[600px]">
      <div className="border border-sc-fg-subtle/20 rounded-xl">
        <LoadingState variant="default" size="md" message="Default" />
      </div>
      <div className="border border-sc-fg-subtle/20 rounded-xl">
        <LoadingState variant="orbital" size="md" message="Orbital" />
      </div>
      <div className="border border-sc-fg-subtle/20 rounded-xl">
        <LoadingState variant="gradient" size="md" message="Gradient" />
      </div>
    </div>
  ),
};

export const ProgressSpinnerDemo: StoryObj = {
  render: function ProgressDemo() {
    const [progress, setProgress] = useState(65);

    return (
      <div className="space-y-8">
        <div className="flex items-center gap-8">
          <ProgressSpinner progress={25} size="sm" />
          <ProgressSpinner progress={50} size="md" />
          <ProgressSpinner progress={75} size="lg" />
          <ProgressSpinner progress={100} size="xl" />
        </div>
        <div className="flex items-center gap-4">
          <input
            type="range"
            min="0"
            max="100"
            value={progress}
            onChange={e => setProgress(Number(e.target.value))}
            className="w-48"
          />
          <ProgressSpinner progress={progress} size="lg" />
        </div>
      </div>
    );
  },
};

export const SkeletonBasic: StoryObj = {
  render: () => (
    <div className="space-y-4 w-[300px]">
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-3/4" />
      <Skeleton className="h-4 w-1/2" />
      <div className="flex items-center gap-3">
        <Skeleton className="h-10 w-10 rounded-full" />
        <div className="flex-1 space-y-2">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-3 w-32" />
        </div>
      </div>
    </div>
  ),
};

export const SkeletonNoShimmer: StoryObj = {
  render: () => (
    <div className="space-y-4 w-[300px]">
      <Skeleton className="h-4 w-full" shimmer={false} />
      <Skeleton className="h-4 w-3/4" shimmer={false} />
      <Skeleton className="h-4 w-1/2" shimmer={false} />
    </div>
  ),
};

export const SkeletonCardDemo: StoryObj = {
  render: () => (
    <div className="grid grid-cols-2 gap-4 w-[500px]">
      <SkeletonCard />
      <SkeletonCard />
    </div>
  ),
};

export const SkeletonListDemo: StoryObj = {
  render: () => (
    <div className="w-[500px]">
      <SkeletonList count={3} />
    </div>
  ),
};
