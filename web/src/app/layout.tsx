import type { Metadata, Viewport } from 'next';
import { Fira_Code, Space_Grotesk } from 'next/font/google';
import { PublicEnvScript } from 'next-dynenv';
import type { ReactNode } from 'react';

import { Providers } from '@/components/providers';

import './globals.css';

const spaceGrotesk = Space_Grotesk({
  variable: '--font-space-grotesk',
  subsets: ['latin'],
  display: 'swap',
});

const firaCode = Fira_Code({
  variable: '--font-fira-code',
  subsets: ['latin'],
  display: 'swap',
});

export const metadata: Metadata = {
  title: 'Sibyl - Knowledge Oracle',
  description: 'Knowledge graph visualization and management for development wisdom',
};

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  themeColor: '#0a0812',
};

// Inline script to prevent FOUC - runs before React hydrates
const themeScript = `
(function() {
  const stored = localStorage.getItem('sibyl-theme');
  let theme = 'neon';
  if (stored === 'neon' || stored === 'dawn') {
    theme = stored;
  } else if (stored === 'system' || !stored) {
    theme = window.matchMedia('(prefers-color-scheme: light)').matches ? 'dawn' : 'neon';
  }
  document.documentElement.setAttribute('data-theme', theme);
})();
`;

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        {/* biome-ignore lint/security/noDangerouslySetInnerHtml: FOUC prevention requires inline script */}
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
        <PublicEnvScript />
      </head>
      <body className={`${spaceGrotesk.variable} ${firaCode.variable} antialiased bg-sc-bg-dark`}>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
