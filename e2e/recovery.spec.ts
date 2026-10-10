import { expect, test } from '@playwright/test';
import { addTextRegion, openEditor, preparedDrawing, saved, serverDocument } from './fixtures';

test('offline edits that conflict with another editor are preserved and offered for recovery', async ({page, context, browser, request}) => {
  const {id} = await preparedDrawing(request);
  await openEditor(page, id);
  await addTextRegion(page, [60, 80, 300, 100], 'SHARED');
  await saved(page);
  await context.setOffline(true);
  await page.locator('input.transcription').fill('MINE');
  await expect(page.locator('.save-indicator').first()).toHaveText('Offline — changes saved on this device', {timeout: 15_000});
  await page.waitForTimeout(9_500);  // the offline tab's lease (8 s in E2E) expires

  const other = await browser.newContext();
  const b = await other.newPage();
  await openEditor(b, id);
  await b.getByRole('tab', {name: /Text regions/}).click();
  await b.locator('.list-row', {hasText: 'SHARED'}).click();
  await b.locator('input.transcription').fill('THEIRS');
  await saved(b);
  await other.close();  // releases its lease

  await context.setOffline(false);
  const dialog = page.getByRole('dialog', {name: /changed before your edits were saved/});
  await expect(dialog).toBeVisible({timeout: 30_000});
  await expect(dialog.getByText(/transcription/)).toBeVisible();
  await dialog.getByRole('button', {name: /Apply 1 selected change/}).click();
  await saved(page);
  const doc = await serverDocument(request, id);
  expect(doc.regions[0].transcription).toBe('MINE');
});

test('typing while an earlier save is delayed never loses newer text', async ({page, request}) => {
  const {id} = await preparedDrawing(request);
  await openEditor(page, id);
  let delayed = false;
  await page.route('**/mutations', async route => {
    if (!delayed) {
      delayed = true;
      await new Promise(r => setTimeout(r, 2500));
    }
    await route.continue();
  });
  await addTextRegion(page, [60, 80, 300, 100], 'A');
  await page.locator('input.transcription').pressSequentially('BCDEF', {delay: 80});
  await saved(page);
  await expect(page.locator('input.transcription')).toHaveValue('ABCDEF');
  expect((await serverDocument(request, id)).regions[0].transcription).toBe('ABCDEF');
});

test('unavailable browser storage is shown and online saving still works', async ({page, request}) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'indexedDB', {get: () => undefined});
  });
  const {id} = await preparedDrawing(request);
  await openEditor(page, id);
  await addTextRegion(page, [60, 80, 300, 100], 'ONLINE ONLY');
  await expect(page.getByText('Local recovery unavailable')).toBeVisible({timeout: 10_000});
  await saved(page);
  expect((await serverDocument(request, id)).regions[0].transcription).toBe('ONLINE ONLY');
});
