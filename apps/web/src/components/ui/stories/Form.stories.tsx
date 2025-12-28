import type { Meta, StoryObj } from '@storybook/nextjs-vite';
import { useState } from 'react';
import { Checkbox } from '../checkbox';
import { FormField, FormFieldInline, FormSection } from '../form-field';
import { Input } from '../input';
import { RadioGroup, RadioGroupItem } from '../radio-group';
import { Switch } from '../switch';

const meta = {
  title: 'UI/Form',
  parameters: {
    layout: 'centered',
  },
  tags: ['autodocs'],
} satisfies Meta;

export default meta;

export const CheckboxExamples: StoryObj = {
  render: () => (
    <div className="space-y-4 min-w-[300px]">
      <Checkbox label="Accept terms and conditions" />
      <Checkbox
        label="Subscribe to newsletter"
        description="Get weekly updates about new features"
      />
      <Checkbox label="Disabled option" disabled />
      <Checkbox label="Checked and disabled" defaultChecked disabled />
    </div>
  ),
};

export const RadioGroupExamples: StoryObj = {
  render: function RadioDemo() {
    const [value, setValue] = useState('option1');
    return (
      <RadioGroup value={value} onValueChange={setValue}>
        <RadioGroupItem value="option1" label="Option 1" description="First choice" />
        <RadioGroupItem value="option2" label="Option 2" description="Second choice" />
        <RadioGroupItem value="option3" label="Option 3" />
      </RadioGroup>
    );
  },
};

export const SwitchExamples: StoryObj = {
  render: () => (
    <div className="space-y-4 min-w-[300px]">
      <Switch label="Enable notifications" size="sm" />
      <Switch label="Dark mode" size="md" description="Use dark theme across the app" />
      <Switch label="Large switch" size="lg" />
      <Switch label="Disabled" disabled />
    </div>
  ),
};

export const FormFieldExample: StoryObj = {
  render: () => (
    <FormSection
      title="Account Settings"
      description="Manage your account preferences"
      className="min-w-[400px]"
    >
      <FormField label="Display Name" description="This will be shown publicly" required>
        <Input placeholder="Enter your name" />
      </FormField>
      <FormField label="Email" error="Email is already taken">
        <Input placeholder="you@example.com" />
      </FormField>
      <FormFieldInline label="Enable notifications">
        <Switch />
      </FormFieldInline>
      <FormFieldInline label="Make profile public">
        <Switch />
      </FormFieldInline>
    </FormSection>
  ),
};

export const AllControls: StoryObj = {
  render: function AllControls() {
    const [checked, setChecked] = useState(false);
    const [radio, setRadio] = useState('a');
    const [switched, setSwitched] = useState(false);

    return (
      <div className="space-y-8 min-w-[400px]">
        <div>
          <h3 className="text-lg font-medium text-sc-fg-primary mb-4">Checkboxes</h3>
          <div className="space-y-2">
            <Checkbox
              checked={checked}
              onCheckedChange={c => setChecked(c === true)}
              label="Controlled checkbox"
            />
          </div>
        </div>

        <div>
          <h3 className="text-lg font-medium text-sc-fg-primary mb-4">Radio Group</h3>
          <RadioGroup value={radio} onValueChange={setRadio}>
            <RadioGroupItem value="a" label="Option A" />
            <RadioGroupItem value="b" label="Option B" />
            <RadioGroupItem value="c" label="Option C" />
          </RadioGroup>
        </div>

        <div>
          <h3 className="text-lg font-medium text-sc-fg-primary mb-4">Switches</h3>
          <Switch checked={switched} onCheckedChange={setSwitched} label="Controlled switch" />
        </div>
      </div>
    );
  },
};
