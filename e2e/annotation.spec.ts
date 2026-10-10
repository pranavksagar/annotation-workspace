import { expect, test } from '@playwright/test';
import { addTextRegion, clickAt, openEditor, preparedDrawing, saved, serverDocument } from './fixtures';

test('library is the landing screen with exactly three workflow statuses and no login or export', async ({page}) => {
  await page.goto('/');
  await expect(page.getByRole('heading', {name: 'Drawings'})).toBeVisible();
  await expect(page.getByRole('tab')).toHaveText([/^All/, /^TO DO/, /^IN PROGRESS/, /^COMPLETED/]);
  await expect(page.getByRole('button', {name: /sign in|log in|export|approve|review|train/i})).toHaveCount(0);
  await expect(page.getByText(/pending approval|needs review/i)).toHaveCount(0);
});

test('annotate half a drawing, leave, resume; another browser sees every saved edit but cannot edit', async ({page, request, browser}) => {
  const {id} = await preparedDrawing(request, 2);
  await openEditor(page, id);
  await addTextRegion(page, [330, 270, 380, 295], 'Ø20');
  await addTextRegion(page, [385, 262, 420, 275], '+0.25');
  await saved(page);
  // Group both as one requirement.
  await page.getByRole('button', {name: 'Select'}).click();
  await clickAt(page, 355, 282);
  await clickAt(page, 402, 268, ['Shift']);
  await page.getByRole('button', {name: 'Group as requirement'}).first().click();
  await expect(page.locator('.badge text')).toHaveText('1', {timeout: 15_000});
  await saved(page);
  let doc = await serverDocument(request, id);
  expect(doc.regions.map((r: {transcription: string}) => r.transcription).sort()).toEqual(['+0.25', 'Ø20']);
  expect(doc.groups).toHaveLength(1);
  expect(doc.annotation_status).toBe('IN_PROGRESS');

  // Move to page 2, then leave through Back and come back: page and data restore.
  await page.getByRole('button', {name: /Page 2/}).click();
  await page.waitForTimeout(1500);  // navigation state is debounced by one second
  await page.getByRole('button', {name: 'Back to drawings'}).click();
  await expect(page.getByRole('heading', {name: 'Drawings'})).toBeVisible();
  await expect(page.locator('.drawing-row', {hasText: 'IN PROGRESS'}).first()).toBeVisible();
  await page.goto(`/#/drawings/${id}`);
  await expect(page.getByRole('button', {name: /Page 2/})).toHaveAttribute('aria-current', 'page');

  // Another browser (separate storage) views the saved work read-only while this tab holds the lease.
  const other = await browser.newContext();
  const viewer = await other.newPage();
  await viewer.goto(`/#/drawings/${id}`);
  await expect(viewer.getByText('Someone is editing this drawing. You can view it or try again shortly.')).toBeVisible({timeout: 20_000});
  await expect(viewer.getByRole('button', {name: 'Text region'})).toBeDisabled();
  await viewer.getByRole('button', {name: /Page 1/}).click();
  await viewer.getByRole('tab', {name: /Requirements/}).click();
  await expect(viewer.locator('.list-row', {hasText: 'Ø20'})).toBeVisible();
  await other.close();
  doc = await serverDocument(request, id);
  expect(doc.regions).toHaveLength(2);
});

test('offline edits are kept on this device and synchronized after reconnecting', async ({page, request, context}) => {
  const {id} = await preparedDrawing(request);
  await openEditor(page, id);
  await context.setOffline(true);
  await addTextRegion(page, [60, 80, 300, 100], 'NOTE A');
  await expect(page.locator('.save-indicator').first()).toHaveText('Offline — changes saved on this device', {timeout: 15_000});
  await context.setOffline(false);
  await saved(page);
  const doc = await serverDocument(request, id);
  expect(doc.regions.map((r: {transcription: string}) => r.transcription)).toEqual(['NOTE A']);
});

test('reload with an unacknowledged save recovers the outbox without duplicating labels', async ({page, request}) => {
  const {id} = await preparedDrawing(request);
  await openEditor(page, id);
  // Let the server commit the first save but lose its response.
  let lost = false;
  await page.route('**/mutations', async route => {
    if (!lost) {
      lost = true;
      await route.fetch();
      await route.abort('connectionreset');
    } else await route.continue();
  });
  await addTextRegion(page, [60, 80, 300, 100], 'LOST RESPONSE');
  await saved(page);  // resolved by resending the same mutation ID (receipt replay)
  let doc = await serverDocument(request, id);
  expect(doc.regions).toHaveLength(1);  // the retried create did not duplicate the region
  expect(doc.regions[0].transcription).toBe('LOST RESPONSE');

  // Now block saves entirely and reload with pending work.
  await page.unroute('**/mutations');
  await page.route('**/mutations', route => route.abort('connectionrefused'));
  await page.locator('input.transcription').fill('LOST RESPONSE EDITED');
  await expect(page.locator('.save-indicator').first()).toHaveText(/Offline|Retrying/, {timeout: 15_000});
  await page.unroute('**/mutations');
  await page.reload();
  await expect(page.locator('.save-indicator').first()).toHaveText(/^Saved/, {timeout: 30_000});
  await expect.poll(async () => (await serverDocument(request, id)).regions.map((r: {transcription: string}) => r.transcription),
    {timeout: 20_000}).toEqual(['LOST RESPONSE EDITED']);
  doc = await serverDocument(request, id);
  expect(doc.regions).toHaveLength(1);
});

test('a duplicated tab cannot take over a live editor', async ({page, context, request}) => {
  const {id} = await preparedDrawing(request);
  await openEditor(page, id);
  const duplicate = await context.newPage();
  await duplicate.goto(`/#/drawings/${id}`);
  await expect(duplicate.getByText('Someone is editing this drawing. You can view it or try again shortly.')).toBeVisible({timeout: 20_000});
  await expect(duplicate.getByRole('button', {name: 'Try editing again'})).toBeVisible();
  await page.close();  // releases the lease
  await duplicate.getByRole('button', {name: 'Try editing again'}).click();
  await expect(duplicate.getByRole('button', {name: 'Text region'})).toBeEnabled({timeout: 20_000});
});

test('finish validates, requires confirmation for empty pages, completes, and can be reopened', async ({page, request}) => {
  const {id} = await preparedDrawing(request, 2);
  await openEditor(page, id);
  await addTextRegion(page, [60, 80, 300, 100], '');
  await saved(page);
  await page.getByRole('button', {name: 'Finish drawing'}).click();
  const dialog = page.getByRole('dialog', {name: 'Finish drawing'});
  await expect(dialog.getByText('Finish confirms that every page has been annotated. Mark unreadable text explicitly.')).toBeVisible();
  await dialog.getByLabel('Page 2 contains no labelable text or symbols').check();
  await dialog.getByRole('button', {name: 'Finish drawing'}).click();
  await expect(dialog.getByText(/needs? attention/)).toBeVisible();
  await dialog.getByRole('button', {name: 'Show'}).first().click();
  await expect(page.locator('input.transcription')).toBeFocused();
  await page.locator('input.transcription').fill('NOTE 1');
  await page.getByRole('button', {name: 'Finish drawing'}).click();
  await dialog.getByLabel('Page 2 contains no labelable text or symbols').check();
  await dialog.getByRole('button', {name: 'Finish drawing'}).click();
  await expect(page.locator('.workspace-header .status')).toHaveText('COMPLETED', {timeout: 15_000});
  await expect(page.getByText('This drawing is completed and available for training.')).toBeVisible();
  await expect(page.getByRole('button', {name: 'Text region'})).toBeDisabled();
  const doc = await serverDocument(request, id);
  expect(doc.annotation_status).toBe('COMPLETED');
  expect(doc.regions[0].transcription).toBe('NOTE 1');

  await expect(page.getByText('Reopening removes this drawing from the current training-ready set until it is finished again.')).toBeVisible();
  await page.getByRole('button', {name: 'Reopen drawing'}).click();
  await expect(page.locator('.workspace-header .status')).toHaveText('IN PROGRESS', {timeout: 15_000});
  await expect(page.getByRole('button', {name: 'Text region'})).toBeEnabled({timeout: 20_000});
});

test('editing geometry: move a region with one committed operation', async ({page, request}) => {
  const {id} = await preparedDrawing(request);
  await openEditor(page, id);
  await addTextRegion(page, [100, 100, 200, 130], 'MOVE ME');
  await saved(page);
  const initial = await serverDocument(request, id);
  const before = initial.regions[0].polygon;
  const version = Number(initial.annotation_version);
  await page.getByRole('button', {name: 'Select'}).click();
  const box = (await page.locator('.canvas-page img').boundingBox())!;
  const at = (x: number, y: number) => [box.x + x / 842 * box.width, box.y + y / 595 * box.height] as const;
  await page.mouse.move(...at(150, 115));
  await page.mouse.down();
  await page.mouse.move(...at(200, 165), {steps: 8});
  await page.mouse.up();
  await saved(page);
  const doc = await serverDocument(request, id);
  expect(Number(doc.annotation_version)).toBe(version + 1);  // one committed operation for the whole drag
  expect(doc.regions[0].polygon[0][0]).toBeGreaterThan(before[0][0] + 40);
  // Undo creates a new inverse operation and moves it back.
  await page.keyboard.press('ControlOrMeta+z');
  await saved(page);
  const undone = await serverDocument(request, id);
  expect(Number(undone.annotation_version)).toBe(version + 2);  // undo is a new operation, never a rollback
  expect(undone.regions[0].polygon).toEqual(before);
});
