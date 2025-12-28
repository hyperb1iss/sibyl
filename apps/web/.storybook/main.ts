import type { StorybookConfig } from '@storybook/nextjs-vite';

const config: StorybookConfig = {
  stories: ['../src/**/*.mdx', '../src/**/*.stories.@(js|jsx|mjs|ts|tsx)'],
  addons: [
    '@chromatic-com/storybook',
    '@storybook/addon-vitest',
    '@storybook/addon-a11y',
    '@storybook/addon-docs',
    '@storybook/addon-onboarding',
  ],
  framework: '@storybook/nextjs-vite',
  staticDirs: ['../public'],
  viteFinal: async (config) => {
    // Suppress noisy warnings that are harmless in Storybook context
    config.build = config.build || {};
    config.build.rollupOptions = config.build.rollupOptions || {};
    const originalOnwarn = config.build.rollupOptions.onwarn;
    config.build.rollupOptions.onwarn = (warning, warn) => {
      // Suppress "use client" directive warnings (Next.js-specific, not needed in Storybook)
      if (
        warning.code === 'MODULE_LEVEL_DIRECTIVE' &&
        warning.message.includes('"use client"')
      ) {
        return;
      }
      // Suppress sourcemap warnings for directive errors
      if (
        warning.code === 'SOURCEMAP_ERROR' &&
        warning.message.includes("Can't resolve original location")
      ) {
        return;
      }
      if (originalOnwarn) {
        originalOnwarn(warning, warn);
      } else {
        warn(warning);
      }
    };
    return config;
  },
};
export default config;