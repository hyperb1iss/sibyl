import type { Preview } from '@storybook/nextjs-vite';
import '../src/app/globals.css';

const preview: Preview = {
  parameters: {
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },
    backgrounds: {
      default: 'dark',
      values: [
        { name: 'dark', value: 'oklch(6% 0.015 285)' },
        { name: 'base', value: 'oklch(10% 0.02 285)' },
        { name: 'elevated', value: 'oklch(17% 0.03 285)' },
      ],
    },
    layout: 'centered',
  },
};

export default preview;
