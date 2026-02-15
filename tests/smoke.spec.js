// @ts-check
const { test, expect } = require('@playwright/test');

/**
 * Helper: set up API route mocks + WebSocket mock so the page loads cleanly.
 * Returns the WebSocket mock server for tests that need WS interaction.
 */
async function setupMocks(page) {
  // Mock all /api/* endpoints with minimal valid responses
  await page.route('**/api/apps', (route) =>
    route.fulfill({ json: { apps: [] } })
  );
  await page.route('**/api/system/volume', (route) =>
    route.fulfill({ json: { volume: 50 } })
  );
  await page.route('**/api/system/brightness', (route) =>
    route.fulfill({ json: { brightness: 50 } })
  );
  await page.route('**/api/clipboard', (route) =>
    route.fulfill({ json: { text: '' } })
  );
  await page.route('**/api/system/info', (route) =>
    route.fulfill({ json: { cpu: 0, memory: 0, os: 'test' } })
  );
  await page.route('**/api/rustdesk/status', (route) =>
    route.fulfill({ json: { running: false } })
  );
  await page.route('**/api/deployment-info', (route) =>
    route.fulfill({ json: { commit_hash: 'test', deployed_at: '' } })
  );
  // Catch-all for any other API routes
  await page.route('**/api/**', (route) =>
    route.fulfill({ json: {} })
  );

  // Mock WebSocket — return a controllable mock server
  const wsMock = await page.routeWebSocket('**/ws', (ws) => {
    ws.onMessage((msg) => {
      try {
        const data = JSON.parse(msg);
        if (data.type === 'ping') {
          ws.send(JSON.stringify({ type: 'pong', t: data.t }));
        }
      } catch {}
    });
  });

  return wsMock;
}

// ─── Test 1: Page loads without JS errors ─────────────────────────────────────
test('page loads without JS errors', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (err) => errors.push(err.message));

  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  expect(errors).toEqual([]);
});

// ─── Test 2: WebSocket state machine ──────────────────────────────────────────
test('WebSocket state machine applies correct CSS classes', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  const dot = page.locator('#status-dot');

  // Connected state — no extra classes
  await expect(dot).not.toHaveClass(/warning|reconnecting|offline/);

  // Simulate slow connection via setConnectionState
  await page.evaluate(() => setConnectionState('slow'));
  await expect(dot).toHaveClass(/warning/);

  // Simulate reconnecting
  await page.evaluate(() => setConnectionState('reconnecting'));
  await expect(dot).toHaveClass(/reconnecting/);
  await expect(dot).not.toHaveClass(/warning/);

  // Simulate disconnected
  await page.evaluate(() => setConnectionState('disconnected'));
  await expect(dot).toHaveClass(/offline/);
  await expect(dot).not.toHaveClass(/reconnecting/);

  // Back to connected
  await page.evaluate(() => setConnectionState('connected'));
  await expect(dot).not.toHaveClass(/warning|reconnecting|offline/);
});

// ─── Test 3: Quick Ribbon renders with mode-grouped buttons ──────────────────
test('Quick Ribbon renders all mode buttons and defaults to terminal', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  // Total buttons across all modes (terminal:3+7, browser:3+2, generic:3+3, shared:3, more:1 = 25)
  const allButtons = page.locator('#quick-ribbon .qr-btn');
  await expect(allButtons).toHaveCount(25);

  // Default mode should be terminal
  const ribbon = page.locator('#quick-ribbon');
  await expect(ribbon).toHaveAttribute('data-mode', 'terminal');

  // Verify terminal primary buttons are visible (display:contents → computed as inline)
  const termGroup = page.locator('.ribbon-group[data-ribbon-mode="terminal"]:not(.ribbon-overflow)');
  await expect(termGroup).toHaveCSS('display', 'contents');

  // Browser and generic primary groups should be hidden
  const browserGroup = page.locator('.ribbon-group[data-ribbon-mode="browser"]:not(.ribbon-overflow)');
  await expect(browserGroup).toHaveCSS('display', 'none');
});

// ─── Test 4: Stream overlay open/close ────────────────────────────────────────
test('stream overlay toggles active class on open/close', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  // Mock the API call that openStream makes
  await page.route('**/api/windows/*/info', (route) =>
    route.fulfill({ json: { type: 'terminal' } })
  );
  await page.route('**/api/windows/*/stream', (route) =>
    route.fulfill({ json: { ok: true } })
  );

  const overlay = page.locator('#stream-overlay');

  // Initially not active
  await expect(overlay).not.toHaveClass(/active/);

  // Open stream
  await page.evaluate(() =>
    streamController.openStream('test-win', 'Test Window', 'cmd.exe')
  );
  await expect(overlay).toHaveClass(/active/);

  // Close stream
  await page.evaluate(() => streamController.closeStream());
  await expect(overlay).not.toHaveClass(/active/);
});

// ─── Test 5: guessWindowType returns correct types ────────────────────────────
test('guessWindowType returns correct types for known window classes', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  const results = await page.evaluate(() => {
    const sc = streamController;
    return {
      cmd: sc.guessWindowType('cmd.exe'),
      powershell: sc.guessWindowType('powershell'),
      terminal: sc.guessWindowType('WindowsTerminal'),
      chrome: sc.guessWindowType('chrome.exe'),
      firefox: sc.guessWindowType('firefox'),
      edge: sc.guessWindowType('msedge'),
      notepad: sc.guessWindowType('notepad.exe'),
      explorer: sc.guessWindowType('explorer'),
      empty: sc.guessWindowType(''),
      nullVal: sc.guessWindowType(null),
    };
  });

  expect(results.cmd).toBe('terminal');
  expect(results.powershell).toBe('terminal');
  expect(results.terminal).toBe('terminal');
  expect(results.chrome).toBe('browser');
  expect(results.firefox).toBe('browser');
  expect(results.edge).toBe('browser');
  expect(results.notepad).toBe('generic');
  expect(results.explorer).toBe('generic');
  expect(results.empty).toBe('generic');
  expect(results.nullVal).toBe('generic');
});

// ─── Test 6: Ribbon mode switching ────────────────────────────────────────────
test('ribbon mode switches show correct button groups', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  const ribbon = page.locator('#quick-ribbon');

  // Switch to browser mode
  await page.evaluate(() => updateRibbonMode('browser'));
  await expect(ribbon).toHaveAttribute('data-mode', 'browser');

  // Browser primary group should be visible, terminal hidden
  const browserGroup = page.locator('.ribbon-group[data-ribbon-mode="browser"]:not(.ribbon-overflow)');
  await expect(browserGroup).toHaveCSS('display', 'contents');
  const termGroup = page.locator('.ribbon-group[data-ribbon-mode="terminal"]:not(.ribbon-overflow)');
  await expect(termGroup).toHaveCSS('display', 'none');

  // Switch to generic mode
  await page.evaluate(() => updateRibbonMode('generic'));
  await expect(ribbon).toHaveAttribute('data-mode', 'generic');
  const genericGroup = page.locator('.ribbon-group[data-ribbon-mode="generic"]:not(.ribbon-overflow)');
  await expect(genericGroup).toHaveCSS('display', 'contents');

  // Switch back to terminal
  await page.evaluate(() => updateRibbonMode('terminal'));
  await expect(ribbon).toHaveAttribute('data-mode', 'terminal');
  await expect(termGroup).toHaveCSS('display', 'contents');
});

// ─── Test 7: Mode picker appears on indicator click ───────────────────────────
test('mode picker appears on indicator click', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  // Mock stream-related APIs
  await page.route('**/api/windows/*/info', (route) =>
    route.fulfill({ json: { type: 'terminal' } })
  );
  await page.route('**/api/windows/*/stream', (route) =>
    route.fulfill({ json: { ok: true } })
  );

  // Open stream to make terminal keyboard (and mode indicator) visible
  await page.evaluate(() =>
    streamController.openStream('test-win', 'Test Window', 'cmd.exe')
  );

  const picker = page.locator('#ribbon-mode-picker');
  await expect(picker).not.toBeVisible();

  // Click the mode indicator
  await page.locator('#ribbon-mode-indicator').click();
  await expect(picker).toBeVisible();

  // Verify picker has 4 options
  const options = picker.locator('button');
  await expect(options).toHaveCount(4);

  await page.evaluate(() => streamController.closeStream());
});

// ─── Test 8: Each mode has 6 primary buttons (3 mode-specific + 3 shared) ────
test('each mode shows 6 primary buttons plus More', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  for (const mode of ['terminal', 'browser', 'generic']) {
    await page.evaluate((m) => updateRibbonMode(m), mode);

    // Count visible primary .qr-btn (using computed visibility)
    const visibleCount = await page.evaluate((m) => {
      const ribbon = document.getElementById('quick-ribbon');
      let count = 0;
      // Count mode-specific primary buttons
      const modeGroup = ribbon.querySelector(`.ribbon-group[data-ribbon-mode="${m}"]:not(.ribbon-overflow)`);
      if (modeGroup) count += modeGroup.querySelectorAll('.qr-btn').length;
      // Count shared buttons
      const shared = ribbon.querySelector('.ribbon-shared');
      if (shared) count += shared.querySelectorAll('.qr-btn').length;
      return count;
    }, mode);

    // 3 mode-specific + 3 shared (Enter, ^C, Esc) = 6
    expect(visibleCount).toBe(6);
  }
});
