import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@/test/utils";

const hooks = vi.hoisted(() => ({
  useSetupStatus: vi.fn(),
  useOnboardingProgress: vi.fn(),
}));
const storage = vi.hoisted(() => ({
  getItem: vi.fn(),
  setItem: vi.fn(),
}));

vi.mock("@/lib/hooks", () => hooks);
vi.mock("@/components/dashboard/connect-agent-modal", () => ({
  ConnectAgentModal: () => <div data-testid="connect-agent-modal" />,
}));
vi.stubGlobal("localStorage", storage);

import { WelcomeBanner } from "./welcome-banner";

describe("WelcomeBanner", () => {
  beforeEach(() => {
    storage.getItem.mockReset();
    storage.setItem.mockReset();
    storage.getItem.mockReturnValue(null);
    hooks.useSetupStatus.mockReturnValue({
      data: {
        openai_valid: false,
        anthropic_valid: false,
      },
    });
    hooks.useOnboardingProgress.mockReturnValue({
      checklist: {
        connected_agent: false,
        added_source: false,
        tried_search: false,
      },
      markConnectedAgent: vi.fn(),
      markAddedSource: vi.fn(),
      markTriedSearch: vi.fn(),
    });
  });

  it("frames onboarding as local-first before MCP setup", () => {
    render(<WelcomeBanner totalEntities={0} />);

    expect(screen.getByText(/local-first where possible/i)).toBeInTheDocument();
    expect(screen.getByText(/local stack first/i)).toBeInTheDocument();
    expect(screen.getByText(/optional when you are ready/i)).toBeInTheDocument();
  });
});
