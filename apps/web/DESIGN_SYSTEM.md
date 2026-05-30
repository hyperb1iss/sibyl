# SilkCircuit Design System

Sibyl's design language: electric meets elegant. Neon hues over deep purple-black.

## Color Palette

### Core Colors (OKLCH)

| Token           | OKLCH (neon)          | Usage                                          |
| --------------- | --------------------- | ---------------------------------------------- |
| `--sc-purple`   | `oklch(64% 0.31 328)` | Primary actions, "doing", keywords, importance |
| `--sc-magenta`  | `oklch(70% 0.32 328)` | Secondary accent, `topic` entity               |
| `--sc-cyan`     | `oklch(92% 0.16 180)` | Interactions, focus, links, "todo"             |
| `--sc-coral`    | `oklch(72% 0.22 350)` | Data, hashes, numbers                          |
| `--sc-yellow`   | `oklch(95% 0.13 105)` | Warnings, "review", attention                  |
| `--sc-green`    | `oklch(88% 0.23 145)` | Success, "done", confirmations                 |
| `--sc-red`      | `oklch(68% 0.22 25)`  | Errors, danger, "blocked"                      |
| `--sc-orange`   | `oklch(78% 0.16 60)`  | Epic identity, `guide`/`epic` entities         |
| `--sc-on-accent`| `oklch(100% 0 0)`     | Text/icons on saturated fills (the only white) |

### Background Hierarchy

| Token               | OKLCH (neon)           | Role (both themes)                          |
| ------------------- | ---------------------- | ------------------------------------------- |
| `--sc-bg-dark`      | `oklch(6% 0.015 285)`  | Page body background                        |
| `--sc-bg-base`      | `oklch(10% 0.02 285)`  | Sidebar, page containers, NON-card panels   |
| `--sc-bg-elevated`  | `oklch(17% 0.03 285)`  | **Cards, modals, dropdowns** (the pop surface) |
| `--sc-bg-highlight` | `oklch(14% 0.025 285)` | Hover states, count chips, inset pills      |
| `--sc-bg-surface`   | `oklch(21% 0.035 285)` | Active states, deep wells, dividers         |

In dawn the lightness ladder inverts: `bg-dark` is the lavender page (95%),
`bg-base` is 97%, and `bg-elevated` is pure white (100%). **Put cards/modals on
`bg-sc-bg-elevated`, never `bg-sc-bg-base`** — on dawn a card on `base` (97%)
does not separate from the page.

### Foreground

| Token               | OKLCH (neon)           | Usage                                   |
| ------------------- | ---------------------- | --------------------------------------- |
| `--sc-fg-primary`   | `oklch(98% 0.005 110)` | Body text, headings                     |
| `--sc-fg-secondary` | `oklch(80% 0.02 280)`  | Section titles, mid-tier labels         |
| `--sc-fg-muted`     | `oklch(62% 0.035 280)` | Secondary text, captions, hints         |
| `--sc-fg-subtle`    | `oklch(42% 0.03 280)`  | Borders / dividers / disabled ONLY      |

`--sc-fg-subtle` is a border tone (≈2.8:1 in dawn) — do not use it for real
informational text; that is `--sc-fg-muted` or stronger.

### Entity & status colors

Entity-type colors are themed CSS variables — `--entity-<type>` (e.g.
`--entity-task`, `--entity-error-pattern`) — each mapping to a core token, and
exposed as Tailwind utilities (`bg-entity-task`, `text-entity-task`,
`border-entity-task/30`). They are the canonical source for entity color and
adapt across themes. Use `getEntityStyles(type)` / `getEntityColorVar(type)`
from `@/lib/constants`; never inline raw hex. Status maps
(`TASK_STATUS_CONFIG`, `EPIC_STATUS_CONFIG`, `CRAWL_STATUS_CONFIG`) follow the
same shape (`color: var(--sc-*)`, `bg-sc-*/20`, `text-sc-*`). The force-graph
canvas is the one sanctioned exception: it cannot read CSS variables, so it
uses theme-keyed hex maps (`CANVAS_COLORS`, `canvasNodeColor`).

## Theming

The web app ships two themes, switched via a `data-theme` attribute on the root:

- `neon` is the dark default. Deep purple-black backgrounds, full-saturation neon
  accents. The OKLCH values in the tables above are the `neon` palette.
- `dawn` is the light theme. Soft lavender-gray backgrounds, deeper accent hues
  tuned for contrast on light surfaces.

```tsx
import { ThemeToggle } from "@/components/ui/theme-toggle";

// Cycles neon and dawn (also bound to Cmd+Shift+L)
<ThemeToggle />;
```

Every `--sc-*` token is redefined per theme, so design-token usage adapts
automatically. Hardcoded Tailwind colors do not, which is one more reason to
stay on the tokens.

## Tailwind Usage

```tsx
// Colors
className = "text-sc-purple bg-sc-bg-base border-sc-fg-subtle/20";

// With opacity
className = "bg-sc-purple/20 text-sc-cyan/80";

// Semantic backgrounds
className = "bg-sc-bg-elevated hover:bg-sc-bg-highlight";
```

## Focus States

All interactive elements use consistent focus styles — cyan ring-2 with the
offset against the surface the control sits on (`-elevated` on cards, the
prevalent case; `-base` on the page/sidebar):

```tsx
className =
  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated";
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
<Card variant="bordered">Transparent, ringed border</Card>
<Card variant="error">Error state</Card>
<Card variant="warning">Warning state</Card>
<Card variant="success">Success state</Card>

<CollapsibleCard title="Expandable" defaultOpen>
  Collapsible content with animation
</CollapsibleCard>
```

### Dialog

```tsx
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
  DialogClose,
} from "@/components/ui";

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
</Dialog>;

// Sizes: sm, md, lg, xl, full
```

### Select

```tsx
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
  SelectGroup,
  SelectLabel,
} from "@/components/ui";

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
</Select>;
```

### DropdownMenu

```tsx
import {
  DropdownMenu,
  DropdownMenuTrigger,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuCheckboxItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
} from "@/components/ui";

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
</DropdownMenu>;
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
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
  TableEmpty,
} from "@/components/ui";

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
      <TableEmpty icon={<SearchIcon />} title="No results" description="Try a different search" />
    ) : (
      items.map((item) => (
        <TableRow key={item.id} interactive selected={selected === item.id}>
          <TableCell>{item.name}</TableCell>
          <TableCell>{item.status}</TableCell>
        </TableRow>
      ))
    )}
  </TableBody>
</Table>;
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
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui";

<Tabs defaultValue="tab1" variant="underline">
  <TabsList>
    <TabsTrigger value="tab1">Account</TabsTrigger>
    <TabsTrigger value="tab2">Settings</TabsTrigger>
  </TabsList>
  <TabsContent value="tab1">Account content</TabsContent>
  <TabsContent value="tab2">Settings content</TabsContent>
</Tabs>;

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

### Progress

```tsx
import { Progress, ScoreBar, CircularProgress } from '@/components/ui';

// Linear progress bar
<Progress value={62} max={100} />

// Score bar for graded values (0 to 1)
<ScoreBar score={0.82} size="sm" />

// Circular progress indicator
<CircularProgress value={45} size={48} />
```

### Spinner and Loading States

```tsx
import {
  Spinner,
  LoadingState,
  Skeleton,
  SkeletonCard,
  SkeletonList,
} from '@/components/ui';

// Inline spinner
<Spinner size="md" color="purple" />

// Full loading state with message
<LoadingState message="Loading memory..." />

// Shaped skeletons for suspense fallbacks
<Skeleton className="h-4 w-32" />
<SkeletonCard />
<SkeletonList count={5} />
```

### Toggle and Chips

```tsx
import { Toggle, FilterChip, TagChip, EntityTypeChip } from '@/components/ui';

// Switch-style toggle with optional label
<Toggle checked={on} onChange={setOn} label="Enable" size="md" />

// Filter chip for facet selection
<FilterChip active={selected} onClick={() => {}}>Patterns</FilterChip>

// Tag chip
<TagChip tag="auth" active={false} onClick={() => {}} />

// Entity-type chip with icon, color, and optional count
<EntityTypeChip entityType="pattern" active onClick={() => {}} count={12} />
```

### Markdown

```tsx
import { Markdown } from '@/components/ui';

// Renders markdown with SilkCircuit-styled typography and code blocks
<Markdown content={entity.description} />
```

## Animations

Built-in animation classes:

```tsx
className = "animate-fade-in"; // Opacity 0 -> 1
className = "animate-slide-up"; // Slide from below
className = "animate-pulse-glow"; // Subtle purple glow
className = "animate-shimmer"; // Loading shimmer
```

## Typography

```tsx
// Font families
className = "font-sans"; // Space Grotesk
className = "font-mono"; // Fira Code

// Text colors
className = "text-sc-fg-primary"; // White (98%)
className = "text-sc-fg-muted"; // Muted purple-gray
className = "text-sc-fg-subtle"; // Subtle, borders
```

## Hard Rules

These are enforced; a change is not done until it holds in **both** themes.

1. **No raw hex in `className` or inline `style`.** Every color resolves to a
   `--sc-*` token, an `--entity-*` var, or `color-mix` on one. Banned:
   `bg-[#…]`, `text-[#…]`, inline `rgba()`, hardcoded tailwind grays
   (`bg-gray-*`). The only exceptions are the force-graph canvas (theme-keyed
   hex maps) and code-language brand colors.
2. **No native OS chrome.** No `window.confirm`/`window.alert` (use
   `ConfirmDialog` / `toast`), no native `<select>` (use `Select`), no
   hand-rolled `fixed inset-0` modals (use `Dialog`).
3. **Locked radius:** `rounded` (chips/kbd) · `rounded-lg` (buttons/inputs) ·
   `rounded-xl` (cards/panels/dialogs) · `rounded-full` (pills/dots). No
   `rounded-2xl` / `rounded-md`.
4. **Cards/modals use `bg-sc-bg-elevated`**, scrims use `bg-sc-bg-dark/70-80`
   (never `bg-black/*`), glows use `shadow-glow-*` (never baked `rgba()`).
5. **Every interactive element** gets the locked focus ring and an accessible
   name. Card-title hover never goes to `text-white` (invisible on dawn).

## Best Practices

### 1. Always use design tokens

```tsx
// Good
className = "bg-sc-bg-elevated text-sc-fg-primary";

// Bad
className = "bg-gray-800 text-white";
```

### 2. Consistent focus states

Copy the standard focus ring to all interactive elements (offset against the
host surface — `-elevated` on cards, `-base` on the page):

```tsx
"focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sc-cyan focus-visible:ring-offset-2 focus-visible:ring-offset-sc-bg-elevated";
```

### 3. Use Radix primitives

All complex interactions should use Radix UI primitives for accessibility:

- Dialog, Select, DropdownMenu, Tooltip (already wrapped)
- Checkbox, RadioGroup, Switch (already wrapped)

### 4. Motion with purpose

Use Framer Motion (`motion/react`) for meaningful transitions:

```tsx
import { motion, AnimatePresence } from "motion/react";

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
</AnimatePresence>;
```

### 5. Responsive by default

Use Tailwind breakpoints consistently:

```tsx
className = "p-4 md:p-6 lg:p-8";
className = "grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3";
```
