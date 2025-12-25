# SilkCircuit Design System

Sibyl's design language: electric meets elegant. Neon hues over deep purple-black.

## Color Palette

### Core Colors (OKLCH)

| Token | OKLCH | Usage |
|-------|-------|-------|
| `--sc-purple` | `oklch(64% 0.31 328)` | Primary actions, keywords, importance |
| `--sc-magenta` | `oklch(70% 0.32 328)` | Secondary accent |
| `--sc-cyan` | `oklch(92% 0.16 180)` | Interactions, focus, links |
| `--sc-coral` | `oklch(72% 0.22 350)` | Data, hashes, numbers |
| `--sc-yellow` | `oklch(95% 0.13 105)` | Warnings, attention |
| `--sc-green` | `oklch(88% 0.23 145)` | Success, confirmations |
| `--sc-red` | `oklch(68% 0.22 25)` | Errors, danger |

### Background Hierarchy

| Token | OKLCH | Usage |
|-------|-------|-------|
| `--sc-bg-dark` | `oklch(6% 0.015 285)` | Page background |
| `--sc-bg-base` | `oklch(10% 0.02 285)` | Cards, containers |
| `--sc-bg-highlight` | `oklch(14% 0.025 285)` | Hover states |
| `--sc-bg-elevated` | `oklch(17% 0.03 285)` | Modals, dropdowns |
| `--sc-bg-surface` | `oklch(21% 0.035 285)` | Active states |

### Foreground

| Token | OKLCH | Usage |
|-------|-------|-------|
| `--sc-fg-primary` | `oklch(98% 0.005 110)` | Primary text |
| `--sc-fg-muted` | `oklch(62% 0.035 280)` | Secondary text |
| `--sc-fg-subtle` | `oklch(42% 0.03 280)` | Disabled, borders |

## Tailwind Usage

```tsx
// Colors
className="text-sc-purple bg-sc-bg-base border-sc-fg-subtle/20"

// With opacity
className="bg-sc-purple/20 text-sc-cyan/80"

// Semantic backgrounds
className="bg-sc-bg-elevated hover:bg-sc-bg-highlight"
```

## Focus States

All interactive elements use consistent focus styles:

```tsx
className="focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base"
```

## Components

### Button

```tsx
import { Button } from '@/components/ui';

<Button variant="primary" size="md">Primary</Button>
<Button variant="secondary">Secondary</Button>
<Button variant="ghost">Ghost</Button>
<Button variant="danger">Danger</Button>
<Button variant="outline">Outline</Button>
<Button variant="link">Link Style</Button>

// Sizes: sm, md, lg
// Props: loading, disabled, leftIcon, rightIcon
```

### Card

```tsx
import { Card, CardHeader, StatCard, CollapsibleCard } from '@/components/ui';

<Card variant="default">Content</Card>
<Card variant="elevated" glow>Elevated with glow</Card>
<Card variant="interactive">Clickable card</Card>
<Card variant="error">Error state</Card>
<Card variant="warning">Warning state</Card>
<Card variant="success">Success state</Card>

<CollapsibleCard title="Expandable" defaultOpen>
  Collapsible content with animation
</CollapsibleCard>
```

### Dialog

```tsx
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter, DialogClose } from '@/components/ui';

<Dialog open={open} onOpenChange={setOpen}>
  <DialogContent size="md">
    <DialogHeader>
      <DialogTitle>Title</DialogTitle>
      <DialogDescription>Description text</DialogDescription>
    </DialogHeader>
    <div>Content</div>
    <DialogFooter>
      <DialogClose asChild>
        <Button variant="secondary">Cancel</Button>
      </DialogClose>
      <Button>Confirm</Button>
    </DialogFooter>
  </DialogContent>
</Dialog>

// Sizes: sm, md, lg, xl, full
```

### Select

```tsx
import { Select, SelectTrigger, SelectValue, SelectContent, SelectItem, SelectGroup, SelectLabel } from '@/components/ui';

<Select value={value} onValueChange={setValue}>
  <SelectTrigger>
    <SelectValue placeholder="Choose..." />
  </SelectTrigger>
  <SelectContent>
    <SelectGroup>
      <SelectLabel>Group</SelectLabel>
      <SelectItem value="a">Option A</SelectItem>
      <SelectItem value="b">Option B</SelectItem>
    </SelectGroup>
  </SelectContent>
</Select>
```

### DropdownMenu

```tsx
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem, DropdownMenuCheckboxItem, DropdownMenuRadioGroup, DropdownMenuRadioItem, DropdownMenuSeparator } from '@/components/ui';

<DropdownMenu>
  <DropdownMenuTrigger asChild>
    <Button>Open</Button>
  </DropdownMenuTrigger>
  <DropdownMenuContent>
    <DropdownMenuItem>Action</DropdownMenuItem>
    <DropdownMenuItem shortcut="Cmd+K">With shortcut</DropdownMenuItem>
    <DropdownMenuSeparator />
    <DropdownMenuCheckboxItem checked={checked} onCheckedChange={setChecked}>
      Toggle
    </DropdownMenuCheckboxItem>
  </DropdownMenuContent>
</DropdownMenu>
```

### Form Components

```tsx
import { Checkbox, RadioGroup, RadioGroupItem, Switch, FormField, FormFieldInline, FormSection } from '@/components/ui';

// Checkbox with label
<Checkbox label="Accept terms" description="Required" />
<Checkbox checked="indeterminate" /> // Indeterminate state

// Radio group
<RadioGroup value={value} onValueChange={setValue}>
  <RadioGroupItem value="a" label="Option A" description="Description" />
  <RadioGroupItem value="b" label="Option B" />
</RadioGroup>

// Switch with sizes
<Switch label="Enable" size="sm" />
<Switch label="Enable" size="md" />
<Switch label="Enable" size="lg" />

// Form layout helpers
<FormSection title="Settings" description="Configure options">
  <FormField label="Name" hint="Your display name" error={error}>
    <Input {...props} />
  </FormField>
  <FormFieldInline label="Active">
    <Switch />
  </FormFieldInline>
</FormSection>
```

### Table

```tsx
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell, TableEmpty } from '@/components/ui';

<Table striped compact>
  <TableHeader>
    <TableRow>
      <TableHead sortable sortDirection="asc" onSort={handleSort}>
        Name
      </TableHead>
      <TableHead>Status</TableHead>
    </TableRow>
  </TableHeader>
  <TableBody>
    {items.length === 0 ? (
      <TableEmpty
        icon={<SearchIcon />}
        title="No results"
        description="Try a different search"
      />
    ) : (
      items.map(item => (
        <TableRow key={item.id} interactive selected={selected === item.id}>
          <TableCell>{item.name}</TableCell>
          <TableCell>{item.status}</TableCell>
        </TableRow>
      ))
    )}
  </TableBody>
</Table>
```

### Avatar

```tsx
import { Avatar, AvatarImage, AvatarFallback, UserAvatar, AvatarGroup } from '@/components/ui';

// Basic
<Avatar size="md">
  <AvatarImage src="/photo.jpg" alt="Name" />
  <AvatarFallback>JD</AvatarFallback>
</Avatar>

// Convenience component
<UserAvatar src="/photo.jpg" name="Jane Doe" size="lg" status="online" />

// Group with overflow
<AvatarGroup max={3}>
  <UserAvatar name="A" />
  <UserAvatar name="B" />
  <UserAvatar name="C" />
  <UserAvatar name="D" /> {/* Shows +1 overflow */}
</AvatarGroup>
```

### Badge

```tsx
import { EntityBadge, StatusBadge, RemovableBadge, BadgeList } from '@/components/ui';

// Entity type badge
<EntityBadge type="pattern" showIcon />

// Status indicator
<StatusBadge status="healthy" pulse />
<StatusBadge status="warning" label="Custom" />

// Removable tags
<BadgeList>
  <RemovableBadge color="purple" onRemove={() => {}}>
    Tag
  </RemovableBadge>
</BadgeList>
```

### Tooltip

```tsx
import { Tooltip, InfoTooltip } from '@/components/ui';

<Tooltip content="Helpful text" side="top">
  <Button>Hover me</Button>
</Tooltip>

// Info icon with tooltip
<InfoTooltip content="Explanation" />
```

### Input

```tsx
import { Input } from '@/components/ui';

<Input placeholder="Text input" />
<Input leftIcon={<SearchIcon />} />
<Input rightIcon={<ClearIcon />} />
<Input error="Invalid value" />
```

### Tabs

```tsx
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui';

<Tabs defaultValue="tab1" variant="underline">
  <TabsList>
    <TabsTrigger value="tab1">Account</TabsTrigger>
    <TabsTrigger value="tab2">Settings</TabsTrigger>
  </TabsList>
  <TabsContent value="tab1">Account content</TabsContent>
  <TabsContent value="tab2">Settings content</TabsContent>
</Tabs>

// Variants: underline, pills, enclosed
```

### Accordion

```tsx
import { Accordion, AccordionItem, AccordionTrigger, AccordionContent, AccordionCard } from '@/components/ui';

// Basic accordion
<Accordion type="single" collapsible>
  <AccordionItem value="item-1">
    <AccordionTrigger icon={<Icon />}>Title</AccordionTrigger>
    <AccordionContent>Content</AccordionContent>
  </AccordionItem>
</Accordion>

// Card-style accordion
<AccordionCard defaultValue="item-1">
  <AccordionCardItem value="item-1">
    <AccordionCardTrigger>Title</AccordionCardTrigger>
    <AccordionCardContent>Content</AccordionCardContent>
  </AccordionCardItem>
</AccordionCard>
```

### Pagination

```tsx
import { Pagination, SimplePagination, PageSizeSelector } from '@/components/ui';

// Full pagination
<Pagination
  currentPage={page}
  totalPages={10}
  onPageChange={setPage}
  size="md"
/>

// Simple prev/next
<SimplePagination
  hasNext={hasMore}
  hasPrev={page > 1}
  onNext={nextPage}
  onPrev={prevPage}
/>

// Page size selector
<PageSizeSelector value={25} onChange={setPageSize} />
```

## Animations

Built-in animation classes:

```tsx
className="animate-fade-in"      // Opacity 0 -> 1
className="animate-slide-up"     // Slide from below
className="animate-pulse-glow"   // Subtle purple glow
className="animate-shimmer"      // Loading shimmer
```

## Typography

```tsx
// Font families
className="font-sans"  // Space Grotesk
className="font-mono"  // Fira Code

// Text colors
className="text-sc-fg-primary"  // White (98%)
className="text-sc-fg-muted"    // Muted purple-gray
className="text-sc-fg-subtle"   // Subtle, borders
```

## Best Practices

### 1. Always use design tokens
```tsx
// Good
className="bg-sc-bg-elevated text-sc-fg-primary"

// Bad
className="bg-gray-800 text-white"
```

### 2. Consistent focus states
Copy the standard focus ring to all interactive elements:
```tsx
"focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-base"
```

### 3. Use Radix primitives
All complex interactions should use Radix UI primitives for accessibility:
- Dialog, Select, DropdownMenu, Tooltip (already wrapped)
- Checkbox, RadioGroup, Switch (already wrapped)

### 4. Motion with purpose
Use Framer Motion (`motion/react`) for meaningful transitions:
```tsx
import { motion, AnimatePresence } from 'motion/react';

<AnimatePresence>
  {visible && (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 10 }}
    >
      Content
    </motion.div>
  )}
</AnimatePresence>
```

### 5. Responsive by default
Use Tailwind breakpoints consistently:
```tsx
className="p-4 md:p-6 lg:p-8"
className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3"
```
