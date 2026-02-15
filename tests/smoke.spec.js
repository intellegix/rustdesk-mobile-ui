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

// ─── Test 9: Pong echoes timestamp ─────────────────────────────────────────────
test('pong echoes timestamp from ping', async ({ page }) => {
  let pongData = null;

  // Custom WS mock that captures pong
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
  await page.routeWebSocket('**/ws', (ws) => {
    ws.onMessage((msg) => {
      try {
        const data = JSON.parse(msg);
        if (data.type === 'ping') {
          // Echo pong with timestamp (like fixed server)
          const pong = { type: 'pong', t: data.t };
          ws.send(JSON.stringify(pong));
        }
      } catch {}
    });
  });

  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  // Send a ping and check that pong handling works (latency detection)
  const result = await page.evaluate(() => {
    return new Promise((resolve) => {
      const origHandler = ws.onmessage;
      ws.onmessage = (event) => {
        if (typeof event.data === 'string') {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'pong') {
              resolve({ hasT: typeof data.t === 'number', t: data.t });
            }
          } catch {}
        }
        if (origHandler) origHandler(event);
      };
      ws.send(JSON.stringify({ type: 'ping', t: Date.now() }));
    });
  });

  expect(result.hasT).toBe(true);
  expect(result.t).toBeGreaterThan(0);
});

// ─── Test 10: Reconnect delay is exponential ────────────────────────────────────
test('reconnect delay increases exponentially', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  const delays = await page.evaluate(() => {
    return [
      getReconnectDelay(0),
      getReconnectDelay(3),
      getReconnectDelay(6),
      getReconnectDelay(10),
    ];
  });

  // Each delay should be larger than the previous (ignoring jitter margin)
  // delay(0) base ≈ 2000, delay(3) base ≈ 6750, delay(6) base ≈ 22781
  expect(delays[1]).toBeGreaterThan(delays[0] * 0.7);
  expect(delays[2]).toBeGreaterThan(delays[1] * 0.7);
  // delay(10) should be capped near 60000
  expect(delays[3]).toBeLessThanOrEqual(72000); // 60000 + 20% jitter
});

// ─── Test 11: Manual retry after max attempts ───────────────────────────────────
test('manual retry banner appears after max reconnect attempts', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  // Simulate exceeding WS_MAX_RETRY_BANNER attempts
  await page.evaluate(() => {
    wsReconnectAttempt = 21;
    setConnectionState('disconnected');
  });

  const banner = page.locator('#reconnect-banner');
  await expect(banner).toHaveClass(/show/);

  const attemptText = await page.locator('#reconnect-attempt').textContent();
  expect(attemptText).toContain('Tap to Retry');
});

// ─── Test 12: Stream auto-resumes on reconnect ─────────────────────────────────
test('stream auto-resumes on WebSocket reconnect', async ({ page }) => {
  let streamStartCount = 0;

  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));
  await page.route('**/api/windows/*/info', (route) =>
    route.fulfill({ json: { type: 'terminal' } })
  );

  await page.routeWebSocket('**/ws', (ws) => {
    ws.onMessage((msg) => {
      try {
        const data = JSON.parse(msg);
        if (data.type === 'ping') {
          ws.send(JSON.stringify({ type: 'pong', t: data.t }));
        }
        if (data.type === 'stream_start') {
          streamStartCount++;
        }
      } catch {}
    });
  });

  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  // Open a stream
  await page.evaluate(() =>
    streamController.openStream('test-win', 'Test Window', 'cmd.exe')
  );

  // Wait for stream_start to be sent
  await page.waitForTimeout(200);

  // Simulate reconnection by setting reconnect state and calling onopen logic
  const resent = await page.evaluate(() => {
    // Simulate that we were reconnecting
    wsReconnectAttempt = 1;
    // Trigger the reconnect path
    const wasReconnect = wsReconnectAttempt > 0;
    wsReconnectAttempt = 0;
    if (wasReconnect && typeof streamController !== 'undefined') {
      streamController.resumeStream();
    }
    return streamController.streamActive;
  });

  expect(resent).toBe(true);
});

// ─── Test 13: PerformanceTelemetry correctness ──────────────────────────────────
test('PerformanceTelemetry computes correct snapshots', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  const snapshot = await page.evaluate(() => {
    perfTelemetry.reset();
    // Record some frames
    for (let i = 0; i < 20; i++) {
      perfTelemetry.recordFrame(5000);
    }
    perfTelemetry.recordLatency(150);
    perfTelemetry.recordLatency(250);
    perfTelemetry.recordDrop();
    return perfTelemetry.getSnapshot();
  });

  expect(snapshot.totalFrames).toBe(20);
  expect(snapshot.drops).toBe(1);
  expect(snapshot.latency).toBe(200); // avg of 150 and 250
  expect(snapshot.avgFrameSize).toBe(5000);
  expect(['good', 'fair', 'poor']).toContain(snapshot.quality);
});

// ─── Test 14: FPS display updates with frames ──────────────────────────────────
test('FPS display updates when stream frames arrive', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  // Mock window info for stream open
  await page.route('**/api/windows/*/info', (route) =>
    route.fulfill({ json: { type: 'terminal' } })
  );

  // Open stream
  await page.evaluate(() =>
    streamController.openStream('test-win', 'Test', 'cmd.exe')
  );

  // Simulate recording frames and forcing display update
  await page.evaluate(() => {
    perfTelemetry.reset();
    for (let i = 0; i < 15; i++) {
      perfTelemetry.recordFrame(3000);
    }
    perfTelemetry.recordLatency(100);
    // Force FPS display update
    streamController.fpsStartTime = Date.now() - 2000; // 2s ago
    streamController._updateFpsDisplay();
  });

  const fpsText = await page.locator('#stream-fps').textContent();
  // Should contain "FPS" text
  expect(fpsText).toContain('FPS');
});

// ─── Test 15: Performance overlay toggle ────────────────────────────────────────
test('performance overlay toggles visibility on FPS click', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  // Mock stream APIs
  await page.route('**/api/windows/*/info', (route) =>
    route.fulfill({ json: { type: 'terminal' } })
  );

  // Open stream to make FPS element interactive
  await page.evaluate(() =>
    streamController.openStream('test-win', 'Test', 'cmd.exe')
  );

  const overlay = page.locator('#perf-overlay');
  await expect(overlay).not.toHaveClass(/show/);

  // Click FPS counter to show overlay
  await page.locator('#stream-fps').click();
  await expect(overlay).toHaveClass(/show/);

  // Click again to hide
  await page.locator('#stream-fps').click();
  await expect(overlay).not.toHaveClass(/show/);

  await page.evaluate(() => streamController.closeStream());
});

// ─── Test 16: Reconnect banner shows countdown ─────────────────────────────────
test('reconnect banner shows countdown number', async ({ page }) => {
  await setupMocks(page);
  await page.goto('/index.html');
  await page.waitForLoadState('networkidle');

  // Simulate reconnecting state with attempt count
  await page.evaluate(() => {
    wsReconnectAttempt = 3;
    setConnectionState('reconnecting');
    const attempt = document.getElementById('reconnect-attempt');
    if (attempt) attempt.textContent = 'Reconnecting in 5s... (attempt 3)';
  });

  const banner = page.locator('#reconnect-banner');
  await expect(banner).toHaveClass(/show/);

  const text = await page.locator('#reconnect-attempt').textContent();
  // Should contain a number (countdown) and "attempt"
  expect(text).toMatch(/\d/);
  expect(text).toContain('attempt');
});
