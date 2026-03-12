# Cortex — Build & Run Instructions

## Project Setup

```bash
# Python environment (using uv)
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"

# Or with pip
pip install -e ".[dev]"

# VS Code extension
cd apps/vscode_extension
pnpm install

# Chrome extension
cd apps/browser_extension
pnpm install
```

## Running Tests

```bash
# Python unit tests
pytest tests/unit/ -v

# Python integration tests
pytest tests/integration/ -v

# All tests with coverage
pytest --cov=services --cov=libs tests/ --cov-report=term-missing

# VS Code extension tests
cd apps/vscode_extension && pnpm test

# Chrome extension tests
cd apps/browser_extension && pnpm test
```

## Development

```bash
# Start all services in dev mode
python scripts/run_dev.py

# Standalone webcam test
python scripts/run_capture.py

# SSH tunnel to gwhiz1 for LLM
bash scripts/setup_ssh_tunnel.sh

# User calibration
python scripts/calibrate.py

# Replay a session for debugging
python scripts/replay_session.py storage/sessions/<session>.jsonl
```

## Build Commands

```bash
# VS Code extension
cd apps/vscode_extension && pnpm run compile

# Chrome extension
cd apps/browser_extension && pnpm run build
```

## Key Dependencies

- **Python:** fastapi, uvicorn, pydantic, opencv-python, mediapipe, scipy, numpy, pynput, PySide6, websockets, structlog, httpx
- **TypeScript:** plasmo, react (Chrome ext), @types/vscode (VS Code ext)
- **Remote:** Qwen-3-8B on gwhiz1.cis.upenn.edu via SSH tunnel

## Key Learnings

- Update this section as the project evolves
- Document any platform-specific gotchas (macOS permissions for pynput, etc.)

## Feature Development Quality Standards

**CRITICAL**: All new features MUST meet the following mandatory requirements before being considered complete.

### Testing Requirements

- **Minimum Coverage**: 85% code coverage ratio required for all new code
- **Test Pass Rate**: 100% - all tests must pass, no exceptions
- **Test Types Required**:
  - Unit tests for all business logic and services
  - Integration tests for API endpoints or main functionality
  - End-to-end tests for critical user workflows
- **Coverage Validation**: Run coverage reports before marking features complete:
  ```bash
  pytest --cov=services --cov=libs tests/ --cov-report=term-missing
  ```
- **Test Quality**: Tests must validate behavior, not just achieve coverage metrics
- **Test Documentation**: Complex test scenarios must include comments explaining the test strategy

### Git Workflow Requirements

Before moving to the next feature, ALL changes must be:

1. **Committed with Clear Messages**:
   ```bash
   git add .
   git commit -m "feat(module): descriptive message following conventional commits"
   ```
   - Use conventional commit format: `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, etc.
   - Include scope when applicable: `feat(physio):`, `fix(state):`, `test(llm):`
   - Write descriptive messages that explain WHAT changed and WHY

2. **Pushed to Remote Repository**:
   ```bash
   git push origin <branch-name>
   ```

3. **Branch Hygiene**:
   - Work on feature branches, never directly on `main`
   - Branch naming convention: `feature/<feature-name>`, `fix/<issue-name>`

4. **Ralph Integration**:
   - Update .ralph/fix_plan.md with new tasks before starting work
   - Mark items complete in .ralph/fix_plan.md upon completion
   - Update .ralph/PROMPT.md if development patterns change

### Feature Completion Checklist

Before marking ANY feature as complete, verify:

- [ ] All tests pass with `pytest`
- [ ] Code coverage meets 85% minimum threshold
- [ ] All changes committed with conventional commit messages
- [ ] .ralph/fix_plan.md task marked as complete
- [ ] .ralph/AGENT.md updated (if new build/test patterns introduced)

**Enforcement**: AI agents should automatically apply these standards to all feature development tasks without requiring explicit instruction for each task.
