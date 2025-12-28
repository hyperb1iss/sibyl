import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { useState } from 'react';
import {
  Accordion,
  AccordionCard,
  AccordionCardContent,
  AccordionCardItem,
  AccordionCardTrigger,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from '../accordion';
import { Database, Settings, User } from '../icons';
import { PageSizeSelector, Pagination, SimplePagination } from '../pagination';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '../tabs';

const meta = {
  title: 'UI/Navigation',
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
} satisfies Meta;

export default meta;

export const TabsUnderline: StoryObj = {
  render: () => (
    <Tabs defaultValue="tab1" variant="underline" className="w-96">
      <TabsList>
        <TabsTrigger value="tab1">Account</TabsTrigger>
        <TabsTrigger value="tab2">Settings</TabsTrigger>
        <TabsTrigger value="tab3">Billing</TabsTrigger>
      </TabsList>
      <TabsContent value="tab1">
        <p className="text-sc-fg-muted">Account settings and preferences.</p>
      </TabsContent>
      <TabsContent value="tab2">
        <p className="text-sc-fg-muted">Configure your application settings.</p>
      </TabsContent>
      <TabsContent value="tab3">
        <p className="text-sc-fg-muted">Manage billing and subscriptions.</p>
      </TabsContent>
    </Tabs>
  ),
};

export const TabsPills: StoryObj = {
  render: () => (
    <Tabs defaultValue="tab1" variant="pills" className="w-96">
      <TabsList>
        <TabsTrigger value="tab1">Overview</TabsTrigger>
        <TabsTrigger value="tab2">Analytics</TabsTrigger>
        <TabsTrigger value="tab3">Reports</TabsTrigger>
      </TabsList>
      <TabsContent value="tab1">
        <p className="text-sc-fg-muted">Dashboard overview content.</p>
      </TabsContent>
      <TabsContent value="tab2">
        <p className="text-sc-fg-muted">Analytics and metrics.</p>
      </TabsContent>
      <TabsContent value="tab3">
        <p className="text-sc-fg-muted">Generated reports.</p>
      </TabsContent>
    </Tabs>
  ),
};

export const TabsEnclosed: StoryObj = {
  render: () => (
    <Tabs defaultValue="tab1" variant="enclosed" className="w-96">
      <TabsList>
        <TabsTrigger value="tab1">Code</TabsTrigger>
        <TabsTrigger value="tab2">Preview</TabsTrigger>
      </TabsList>
      <TabsContent value="tab1">
        <pre className="text-sm text-sc-fg-muted font-mono">
          {`function hello() {\n  console.log("Hello!");\n}`}
        </pre>
      </TabsContent>
      <TabsContent value="tab2">
        <p className="text-sc-fg-muted">Live preview would appear here.</p>
      </TabsContent>
    </Tabs>
  ),
};

export const AccordionBasic: StoryObj = {
  render: () => (
    <Accordion type="single" collapsible className="w-96">
      <AccordionItem value="item-1">
        <AccordionTrigger>What is Sibyl?</AccordionTrigger>
        <AccordionContent>
          Sibyl is a Graph-RAG Knowledge Oracle that provides AI agents with development wisdom
          through a Graphiti-powered knowledge graph.
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value="item-2">
        <AccordionTrigger>How does it work?</AccordionTrigger>
        <AccordionContent>
          Sibyl stores knowledge as entities in FalkorDB, connected by semantic relationships,
          enabling intelligent retrieval and context-aware responses.
        </AccordionContent>
      </AccordionItem>
      <AccordionItem value="item-3">
        <AccordionTrigger>What technologies are used?</AccordionTrigger>
        <AccordionContent>
          Python, FastMCP, FastAPI, Graphiti, FalkorDB on the backend. Next.js, React Query, and
          Tailwind on the frontend.
        </AccordionContent>
      </AccordionItem>
    </Accordion>
  ),
};

export const AccordionWithIcons: StoryObj = {
  render: () => (
    <Accordion type="single" collapsible className="w-96">
      <AccordionItem value="item-1">
        <AccordionTrigger icon={<User className="w-5 h-5" />}>Account</AccordionTrigger>
        <AccordionContent>Manage your account settings and preferences.</AccordionContent>
      </AccordionItem>
      <AccordionItem value="item-2">
        <AccordionTrigger icon={<Settings className="w-5 h-5" />}>Settings</AccordionTrigger>
        <AccordionContent>Configure application behavior and appearance.</AccordionContent>
      </AccordionItem>
      <AccordionItem value="item-3">
        <AccordionTrigger icon={<Database className="w-5 h-5" />}>Data</AccordionTrigger>
        <AccordionContent>View and manage your stored data.</AccordionContent>
      </AccordionItem>
    </Accordion>
  ),
};

export const AccordionCardStyle: StoryObj = {
  render: () => (
    <AccordionCard defaultValue="item-1" className="w-96">
      <AccordionCardItem value="item-1">
        <AccordionCardTrigger icon={<Database className="w-5 h-5" />}>
          Database Configuration
        </AccordionCardTrigger>
        <AccordionCardContent>
          Configure your database connection settings, pooling options, and timeouts.
        </AccordionCardContent>
      </AccordionCardItem>
      <AccordionCardItem value="item-2">
        <AccordionCardTrigger icon={<Settings className="w-5 h-5" />}>
          API Settings
        </AccordionCardTrigger>
        <AccordionCardContent>
          Set up API keys, rate limits, and authentication methods.
        </AccordionCardContent>
      </AccordionCardItem>
    </AccordionCard>
  ),
};

export const PaginationExample: StoryObj = {
  render: function PaginationDemo() {
    const [page, setPage] = useState(1);
    return (
      <div className="space-y-8">
        <div>
          <p className="text-sc-fg-muted mb-4">Current page: {page}</p>
          <Pagination currentPage={page} totalPages={10} onPageChange={setPage} />
        </div>
        <div>
          <p className="text-sc-fg-muted mb-4">Small variant:</p>
          <Pagination currentPage={page} totalPages={10} onPageChange={setPage} size="sm" />
        </div>
      </div>
    );
  },
};

export const SimplePaginationExample: StoryObj = {
  render: function SimplePaginationDemo() {
    const [page, setPage] = useState(1);
    const totalPages = 5;
    return (
      <div className="space-y-4">
        <p className="text-sc-fg-muted">
          Page {page} of {totalPages}
        </p>
        <SimplePagination
          hasNext={page < totalPages}
          hasPrev={page > 1}
          onNext={() => setPage(p => p + 1)}
          onPrev={() => setPage(p => p - 1)}
        />
      </div>
    );
  },
};

export const PageSizeSelectorExample: StoryObj = {
  render: function PageSizeDemo() {
    const [size, setSize] = useState(25);
    return (
      <div className="space-y-4">
        <p className="text-sc-fg-muted">Selected: {size} items per page</p>
        <PageSizeSelector value={size} onChange={setSize} />
      </div>
    );
  },
};
