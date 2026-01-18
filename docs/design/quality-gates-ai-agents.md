# Quality Gates for AI Agent Code Generation

> Design patterns for ensuring high-quality AI-generated code without bottlenecking development.

## Executive Summary

AI agents generate code at unprecedented speed, but quality assurance hasn't kept pace. This
document defines five quality gate patterns for different change types, balancing automation with
human oversight based on risk and complexity.

**Core Philosophy**: Gates should be _proportional to risk_. A typo fix doesn't need the same
scrutiny as a payment integration.

---

## Part 1: Research Findings

### 1.1 AI Code Review Capabilities

Modern AI code review tools have matured significantly:

**GitHub Copilot Code Review** (GA April 2025)

- Automatic security scanning with CodeQL
- 80% more comments per PR than 2024
- Integrates ESLint, PMD for quality checks
- Reviews draft PRs and re-reviews on each push

**Claude Code Review Plugin** (Anthropic)

- 4 parallel specialized agents: 2 for guidelines, 1 for bugs, 1 for history
- Confidence scoring (0-100) with 80+ threshold for reporting
- Filters: pre-existing issues, pedantic nitpicks, linter-catchable problems
- Self-improving: Anthropic uses Claude to review Claude-generated code

**Cursor Bugbot**

- Maximum 10 inline comments per review (signal-to-noise optimization)
- Severity indicators: Critical, Security, Performance
- Can block merges on critical/security issues
- Inline fix application directly in IDE

### 1.2 Testing Integration

**SonarQube AI Code Quality Gate** (2025.1 LTA)

```
New Code Conditions:
- No new issues introduced
- All security hotspots reviewed
- Test coverage >= 80%
- Code duplication <= 3%

Overall Code Conditions:
- Security rating: A
- All hotspots reviewed
- Reliability rating: C or better
```

**Key Insight**: AI-generated code requires _stricter_ coverage thresholds because agents may
introduce subtle bugs that humans wouldn't.

### 1.3 Human-in-the-Loop Optimization

**UALA Framework** (Uncertainty-Aware Language Agents) Decision pathways based on uncertainty
quantification:

1. **Accept internally**: Low uncertainty, proceed without tools
2. **Activate tools**: High uncertainty, invoke external validators
3. **Defer to human**: Both paths uncertain, escalate

**Result**: 38.2% exact match on HotpotQA with 50% fewer tool calls than ReAct.

**Human Review Remains Essential For**:

- Architectural decisions beyond automated constraints
- Context-dependent code quality assessments
- Business logic validation
- Security-critical code (cryptography, auth, financial transactions)

### 1.4 Rework Loop Efficiency

**Google Research**: 70% of changes commit within 24 hours of initial review.

**Anti-Pattern**: "Review scope creep" - introducing new issues with each round.

**Best Practice**: Reviewers should focus on whether _original concerns_ were addressed, not adding
new ones (unless critical).

**The Ralph Loop Warning**: Pure LLM feedback loops can cause "iterative safety degradation" -
initially secure code may become vulnerable after multiple AI "improvements."

### 1.5 Confidence Signaling

**Current State**: LLMs are notoriously miscalibrated. When models say "100% confident," they often
fail fact-checks.

**Practical Approaches**:

- Token-level entropy analysis (requires log-probability access)
- Multi-inference sampling (compare multiple outputs)
- Calibration thresholds from training data

**Anthropic's Approach**:

- Score each issue 0-100 independently
- Only report issues >= 80 confidence
- Use specialized agents (security, bugs, guidelines) for domain expertise

---

## Part 2: Quality Gate Patterns

### Pattern 1: Standard Code Change

**Scope**: Bug fixes, small features, incremental improvements

```
┌─────────────────────────────────────────────────────────────────┐
│  STANDARD CHANGE PIPELINE                                       │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Agent writes code                                              │
│         ↓                                                       │
│  ┌──────────────────┐                                          │
│  │ Gate 1: Linting  │ ← REQUIRED, AUTOMATED                    │
│  │ + Type Check     │   Fail-fast, <30 seconds                 │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 2: Unit     │ ← REQUIRED, AUTOMATED                    │
│  │ Tests Pass       │   Coverage must not decrease             │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 3: AI       │ ← REQUIRED, AUTOMATED                    │
│  │ Code Review      │   Confidence >= 80 blocks                │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 4: Human    │ ← OPTIONAL for trusted agents            │
│  │ Review           │   Required for new contributors          │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│       ✓ MERGE                                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

| Attribute          | Value                                  |
| ------------------ | -------------------------------------- |
| Time Budget        | 15-30 minutes                          |
| Human Involvement  | Optional (acceptance testing shift)    |
| Retry Strategy     | Agent fixes issues, max 3 iterations   |
| Failure Escalation | Human review after 3 failed iterations |

**Confidence Signal**:

```python
class ChangeConfidence:
    HIGH = "Agent has solved similar issues before"
    MEDIUM = "New pattern but within known domain"
    LOW = "Novel approach, unfamiliar codebase area"
```

---

### Pattern 2: Heavy UI Feature

**Scope**: Visual changes, new components, design system updates

```
┌─────────────────────────────────────────────────────────────────┐
│  UI FEATURE PIPELINE                                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Agent writes code                                              │
│         ↓                                                       │
│  ┌──────────────────┐                                          │
│  │ Gate 1: Standard │ ← REQUIRED, AUTOMATED                    │
│  │ (Lint/Type/Test) │                                          │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 2: Visual   │ ← REQUIRED, AUTOMATED                    │
│  │ Regression       │   Percy/Applitools with AI diff          │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 3: A11y     │ ← REQUIRED, AUTOMATED                    │
│  │ Scan             │   axe-core, WCAG compliance              │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 4: Visual   │ ← REQUIRED, HUMAN                        │
│  │ Review           │   Screenshots in PR description          │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 5: Design   │ ← OPTIONAL for minor changes             │
│  │ System Check     │   Required for new components            │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│       ✓ MERGE                                                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

| Attribute          | Value                                         |
| ------------------ | --------------------------------------------- |
| Time Budget        | 1-4 hours (visual review is inherently human) |
| Human Involvement  | REQUIRED (visual approval)                    |
| Retry Strategy     | Update screenshots, baseline if intentional   |
| Failure Escalation | Design review for UX concerns                 |

**Evidence Requirements**:

- Before/after screenshots in PR
- Mobile + desktop viewports
- Dark/light mode if applicable
- Interactive states (hover, focus, active)

---

### Pattern 3: Security-Sensitive Change

**Scope**: Authentication, authorization, payments, PII handling, cryptography

```
┌─────────────────────────────────────────────────────────────────┐
│  SECURITY-SENSITIVE PIPELINE                                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Agent writes code                                              │
│         ↓                                                       │
│  ┌──────────────────┐                                          │
│  │ Gate 1: Standard │ ← REQUIRED, AUTOMATED                    │
│  │ + SAST Scan      │   CodeQL, Semgrep, Snyk                  │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 2: Secrets  │ ← REQUIRED, AUTOMATED                    │
│  │ Scanning         │   GitLeaks, TruffleHog                   │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 3: AI       │ ← REQUIRED, AUTOMATED                    │
│  │ Security Review  │   Multi-agent (Endor Labs style)         │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 4: Expert   │ ← REQUIRED, HUMAN                        │
│  │ Security Review  │   Security champion or team              │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 5: Threat   │ ← REQUIRED for auth/payments             │
│  │ Model Review     │   Document attack vectors                │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│       ✓ MERGE (with audit trail)                               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

| Attribute          | Value                                  |
| ------------------ | -------------------------------------- |
| Time Budget        | 4-24 hours (security cannot be rushed) |
| Human Involvement  | MANDATORY (security expert required)   |
| Retry Strategy     | Fix + re-scan, no bypass allowed       |
| Failure Escalation | Security team lead approval            |

**AI Agent Boundaries**:

- Agents CANNOT auto-merge security-sensitive code
- Agents CANNOT modify auth logic without explicit approval
- Agents MUST flag: new API endpoints, PII collection, crypto changes

**Endor Labs Categories** (16+ detected):

- API endpoint modifications
- Authentication/cryptographic changes
- PII data handling
- CI/CD workflow alterations
- Database schema modifications

---

### Pattern 4: Emergency Hotfix

**Scope**: Production is down, critical customer impact, security breach

```
┌─────────────────────────────────────────────────────────────────┐
│  EMERGENCY HOTFIX PIPELINE                                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ⚠️  EMERGENCY DECLARED (documented reason)                     │
│         ↓                                                       │
│  ┌──────────────────┐                                          │
│  │ Gate 1: Smoke    │ ← REQUIRED, AUTOMATED                    │
│  │ Tests Only       │   Critical path only, <5 min             │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 2: Peer     │ ← REQUIRED, HUMAN                        │
│  │ Eyeballs         │   1 engineer (can be async)              │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 3: Manager  │ ← REQUIRED for bypass                    │
│  │ Approval         │   Force-merge authorization              │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│       ✓ DEPLOY (with post-mortem ticket)                       │
│         ↓                                                       │
│  ┌──────────────────┐                                          │
│  │ Gate 4: Full     │ ← REQUIRED, POST-DEPLOY                  │
│  │ Review           │   Within 24-48 hours                     │
│  └──────────────────┘                                          │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

| Attribute          | Value                               |
| ------------------ | ----------------------------------- |
| Time Budget        | 15-60 minutes (incident resolution) |
| Human Involvement  | REQUIRED (bypass authorization)     |
| Retry Strategy     | N/A - first working fix wins        |
| Failure Escalation | Rollback, escalate incident         |

**GitHub Rulesets Pattern** (Swissquote):

- Normal users: Must pass all gates
- Managers: "Allow for pull requests only" bypass with audit log
- System accounts: "Always allow" for automated rollbacks

**Post-Hotfix Requirements**:

1. Create follow-up ticket within 1 hour
2. Full review within 48 hours
3. Add tests that would have caught the issue
4. Update runbooks if applicable

---

### Pattern 5: Refactor

**Scope**: No behavior change, code cleanup, dependency updates, tech debt

```
┌─────────────────────────────────────────────────────────────────┐
│  REFACTOR PIPELINE                                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Agent proposes refactor                                        │
│         ↓                                                       │
│  ┌──────────────────┐                                          │
│  │ Gate 0: Scope    │ ← REQUIRED, HUMAN                        │
│  │ Agreement        │   Agree on boundaries BEFORE starting    │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  Agent executes refactor                                        │
│         ↓                                                       │
│  ┌──────────────────┐                                          │
│  │ Gate 1: Full     │ ← REQUIRED, AUTOMATED                    │
│  │ Test Suite       │   ALL tests must pass (100%)             │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 2: Behavior │ ← REQUIRED, AUTOMATED                    │
│  │ Comparison       │   Snapshot tests, API contracts          │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 3: Perf     │ ← REQUIRED for perf-sensitive code       │
│  │ Regression       │   Benchmark comparison                   │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌──────────────────┐                                          │
│  │ Gate 4: Code     │ ← REQUIRED, HUMAN                        │
│  │ Review           │   Focus on maintainability               │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│       ✓ MERGE (behind feature flag if risky)                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

| Attribute          | Value                              |
| ------------------ | ---------------------------------- |
| Time Budget        | 1-8 hours (depending on scope)     |
| Human Involvement  | REQUIRED (scope + review)          |
| Retry Strategy     | Revert to smaller scope if failing |
| Failure Escalation | Split into smaller PRs             |

**Critical Rules for AI Refactoring**:

1. **NEVER change behavior** - tests are the contract
2. **Scope agreement first** - agent proposes, human approves boundaries
3. **Atomic commits** - each commit is independently revertible
4. **Feature flags** - large refactors deploy behind flags

**The Iterative Safety Degradation Risk**:

> "In pure LLM feedback loops, code safety can systematically degrade with increasing iterations."

Mitigation: Limit refactor iterations, require human checkpoint every 3 iterations.

---

## Part 3: Implementation Guidelines

### 3.1 Confidence Signaling Protocol

Agents should signal confidence at the start of each task:

```typescript
interface ConfidenceSignal {
  level: "high" | "medium" | "low" | "uncertain";
  reasoning: string;

  // Factors considered
  familiarityWithCodebase: number; // 0-100
  similarPastTasks: number; // count
  uncertaintyMarkers: string[]; // "new API", "unfamiliar pattern"

  // Recommended gates
  suggestedGates: Gate[];
  requestsHumanReview: boolean;
}
```

**When to Request Human Review**:

- Novel architectural patterns
- Security-adjacent code
- Performance-critical paths
- External integrations
- Ambiguous requirements

### 3.2 Rework Loop Management

```
┌─────────────────────────────────────────────────────────────────┐
│  REWORK LOOP (max 3 iterations)                                 │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Review feedback received                                       │
│         ↓                                                       │
│  ┌──────────────────┐                                          │
│  │ Categorize       │                                          │
│  │ Feedback         │                                          │
│  └────────┬─────────┘                                          │
│           ↓                                                     │
│  ┌────────────────────────────────────┐                        │
│  │ Original concern?  New issue?       │                        │
│  │ ────────────────   ──────────       │                        │
│  │ Fix immediately    Log for later    │                        │
│  │                    (no scope creep) │                        │
│  └────────┬───────────────────────────┘                        │
│           ↓                                                     │
│  Agent fixes original concerns only                             │
│         ↓                                                       │
│  Re-run failed gates only (not full suite)                      │
│         ↓                                                       │
│  Iteration count++                                              │
│         ↓                                                       │
│  ┌──────────────────┐                                          │
│  │ Iteration > 3?   │─── Yes ──→ ESCALATE TO HUMAN            │
│  └────────┬─────────┘                                          │
│      No   ↓                                                     │
│  Continue loop                                                  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.3 Gate Configuration by Repository Risk Level

| Risk Level   | Example Repos                  | Required Gates        | Time Budget |
| ------------ | ------------------------------ | --------------------- | ----------- |
| **Critical** | payments-service, auth-service | All + threat model    | 4-24h       |
| **High**     | user-service, api-gateway      | All automated + human | 1-4h        |
| **Medium**   | web-app, mobile-app            | Standard + visual     | 30m-2h      |
| **Low**      | internal-tools, docs           | Lint + tests only     | 15-30m      |

### 3.4 Failure Handling Matrix

| Gate Failed       | Retry? | Max Attempts | Escalation              |
| ----------------- | ------ | ------------ | ----------------------- |
| Lint/Types        | Yes    | 3            | Never (must fix)        |
| Unit Tests        | Yes    | 3            | Human review            |
| AI Review (>=80)  | Yes    | 2            | Human review            |
| Visual Regression | No     | N/A          | Human baseline decision |
| Security Scan     | Yes    | 2            | Security team           |
| Human Review      | N/A    | N/A          | Alternative reviewer    |

---

## Part 4: Tool Ecosystem

### AI Code Review Tools (2025)

| Tool                       | Strength                        | Integration   |
| -------------------------- | ------------------------------- | ------------- |
| GitHub Copilot Code Review | Best GitHub integration         | Native        |
| Claude Code Review Plugin  | Confidence scoring, multi-agent | CLI/Actions   |
| Cursor Bugbot              | IDE integration, inline fixes   | VSCode/Cursor |
| Qodo                       | Enterprise scale, Fortune 100   | CI/CD         |
| Endor Labs                 | Security-focused, multi-agent   | GitHub/GitLab |

### Visual Regression

| Tool                 | AI Features                       | Best For            |
| -------------------- | --------------------------------- | ------------------- |
| Percy (BrowserStack) | AI diff, false positive filtering | CI integration      |
| Applitools           | Visual AI, cross-browser          | Enterprise          |
| Reflect              | No-code, generative AI tests      | Fast setup          |
| Chromatic            | Storybook integration             | Component libraries |

### Security Scanning

| Tool      | Coverage               | Integration   |
| --------- | ---------------------- | ------------- |
| CodeQL    | Deep semantic analysis | GitHub native |
| Snyk      | Dependency + SAST      | Universal     |
| Semgrep   | Custom rules, fast     | CLI/CI        |
| Checkmarx | Enterprise SAST        | Enterprise    |

---

## Part 5: Metrics to Track

### Quality Metrics

| Metric                          | Target    | Warning    |
| ------------------------------- | --------- | ---------- |
| Gate pass rate (first attempt)  | > 85%     | < 70%      |
| Mean time to merge              | < 4 hours | > 24 hours |
| Rework iterations per PR        | < 1.5     | > 3        |
| False positive rate (AI review) | < 10%     | > 25%      |
| Post-merge defects              | < 2%      | > 5%       |

### Efficiency Metrics

| Metric                 | Target   | Warning  |
| ---------------------- | -------- | -------- |
| Human review time      | < 15 min | > 1 hour |
| Gate execution time    | < 10 min | > 30 min |
| Time to first feedback | < 5 min  | > 30 min |

---

## References

- [GitHub Copilot Code Review](https://docs.github.com/en/copilot/concepts/agents/code-review) -
  Official documentation
- [Cursor CLI Code Review](https://cursor.com/docs/cli/cookbook/code-review) - Bugbot and automation
- [Claude Code Review Plugin](https://github.com/anthropics/claude-code/blob/main/plugins/code-review/README.md) -
  Multi-agent confidence scoring
- [SonarQube AI Code Quality Gates](https://docs.sonarsource.com/sonarqube-server/2025.1/instance-administration/analysis-functions/ai-code-assurance/quality-gates-for-ai-code/) -
  80% coverage threshold
- [Endor Labs AI Security Code Review](https://www.endorlabs.com/learn/introducing-ai-security-code-review) -
  Multi-agent security analysis
- [UALA: Uncertainty-Aware Language Agents](https://uala-agent.github.io/) - Confidence
  quantification
- [Anthropic Security Reviews with Claude Code](https://www.anthropic.com/news/automate-security-reviews-with-claude-code) -
  Agentic security analysis
- [Graphite AI Code Review](https://graphite.com/blog/how-ai-code-review-reduces-review-cycles) -
  Cycle reduction
- [Autonomous Quality Gates](https://www.augmentcode.com/learn/autonomous-quality-gates-ai-powered-code-review) -
  Policy-as-code
- [GitHub Rulesets](https://medium.com/swissquote-engineering/github-rulesets-because-two-reviewers-are-better-than-a-hotfix-9f03124f1110) -
  Emergency bypass patterns
