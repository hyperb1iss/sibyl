import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen } from '@/test/utils';
import { ApiKeysStep } from './api-keys-step';

// Mock the hooks
const mockUseSettings = vi.fn();
const mockUseUpdateSettings = vi.fn();
const mockUseValidateApiKeys = vi.fn();

vi.mock('@/lib/hooks', () => ({
  useSettings: () => mockUseSettings(),
  useUpdateSettings: () => mockUseUpdateSettings(),
  useValidateApiKeys: () => mockUseValidateApiKeys(),
}));

describe('ApiKeysStep', () => {
  const mockOnBack = vi.fn();
  const mockOnValidated = vi.fn();

  beforeEach(() => {
    mockOnBack.mockClear();
    mockOnValidated.mockClear();

    // Default mock implementations - no keys configured
    mockUseSettings.mockReturnValue({
      data: {
        settings: {
          openai_api_key: { configured: false, masked: null },
          anthropic_api_key: { configured: false, masked: null },
        },
      },
    });

    mockUseUpdateSettings.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
      isError: false,
      data: null,
    });

    mockUseValidateApiKeys.mockReturnValue({
      data: null,
      refetch: vi.fn(),
      isLoading: false,
    });
  });

  describe('Initial State', () => {
    it('renders the configure API keys header', () => {
      render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      expect(screen.getByText('Configure API Keys')).toBeInTheDocument();
    });

    it('shows disabled button when no keys entered', () => {
      render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      const button = screen.getByRole('button', { name: /enter api keys/i });
      expect(button).toBeDisabled();
    });

    it('shows OpenAI and Anthropic input sections', () => {
      render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      // Check for input placeholders instead (more specific)
      expect(screen.getByPlaceholderText('sk-...')).toBeInTheDocument();
      expect(screen.getByPlaceholderText('sk-ant-...')).toBeInTheDocument();
    });
  });

  describe('Configured State', () => {
    it('shows Continue button when both keys are configured', () => {
      mockUseSettings.mockReturnValue({
        data: {
          settings: {
            openai_api_key: { configured: true, masked: 'sk-...abc123' },
            anthropic_api_key: { configured: true, masked: 'sk-ant-...xyz789' },
          },
        },
      });

      render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      const continueButton = screen.getByRole('button', { name: /continue/i });
      expect(continueButton).toBeEnabled();
    });

    it('shows progress message when only OpenAI is configured', () => {
      mockUseSettings.mockReturnValue({
        data: {
          settings: {
            openai_api_key: { configured: true, masked: 'sk-...abc123' },
            anthropic_api_key: { configured: false, masked: null },
          },
        },
      });

      render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      expect(
        screen.getByText(/OpenAI key saved! Now add your Anthropic key/i)
      ).toBeInTheDocument();
    });

    it('shows progress message when only Anthropic is configured', () => {
      mockUseSettings.mockReturnValue({
        data: {
          settings: {
            openai_api_key: { configured: false, masked: null },
            anthropic_api_key: { configured: true, masked: 'sk-ant-...xyz789' },
          },
        },
      });

      render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      expect(
        screen.getByText(/Anthropic key saved! Now add your OpenAI key/i)
      ).toBeInTheDocument();
    });
  });

  describe('Save Flow', () => {
    it('shows Save Key button when user enters a key', async () => {
      const { user } = render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      // Type in the OpenAI key input (password type, use placeholder)
      const openaiInput = screen.getByPlaceholderText('sk-...');
      await user.type(openaiInput, 'sk-test-key');

      // Button should change to "Save Key"
      expect(screen.getByRole('button', { name: /^save key$/i })).toBeEnabled();
    });

    it('shows "Save Keys" when both inputs have values', async () => {
      const { user } = render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      const openaiInput = screen.getByPlaceholderText('sk-...');
      const anthropicInput = screen.getByPlaceholderText('sk-ant-...');

      await user.type(openaiInput, 'sk-test-key');
      await user.type(anthropicInput, 'sk-ant-test-key');

      // Button should say "Save Keys" (plural)
      expect(screen.getByRole('button', { name: /save keys/i })).toBeEnabled();
    });
  });

  describe('Error Handling', () => {
    it('shows error message on save failure', () => {
      mockUseUpdateSettings.mockReturnValue({
        mutateAsync: vi.fn(),
        isPending: false,
        isError: true,
        data: null,
      });

      render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      expect(
        screen.getByText(/failed to save settings/i)
      ).toBeInTheDocument();
    });
  });

  describe('Navigation', () => {
    it('calls onBack when Back button is clicked', async () => {
      const { user } = render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      await user.click(screen.getByRole('button', { name: /back/i }));
      expect(mockOnBack).toHaveBeenCalled();
    });

    it('calls onValidated when Continue is clicked with both keys configured', async () => {
      mockUseSettings.mockReturnValue({
        data: {
          settings: {
            openai_api_key: { configured: true, masked: 'sk-...abc123' },
            anthropic_api_key: { configured: true, masked: 'sk-ant-...xyz789' },
          },
        },
      });

      const { user } = render(
        <ApiKeysStep
          initialStatus={undefined}
          onBack={mockOnBack}
          onValidated={mockOnValidated}
        />
      );

      await user.click(screen.getByRole('button', { name: /continue/i }));
      expect(mockOnValidated).toHaveBeenCalledWith(true);
    });
  });
});
