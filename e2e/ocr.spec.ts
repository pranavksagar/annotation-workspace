import { expect, test, type Page } from '@playwright/test';
import { drawBox, openEditor, preparedDrawing, saved, serverDocument } from './fixtures';

/** OCR is off on the e2e server; switch it on in the browser and stub the engine's answer. */
async function withOcr(page: Page, answer: () => {status: number; body: unknown}) {
  const requests: unknown[] = [];
  await page.route(/\/api\/annotation\/drawings\/[0-9a-f-]{36}$/, async route => {
    const response = await route.fetch();
    await route.fulfill({response, json: {...(await response.json()), ocr_enabled: true}});
  });
  await page.route('**/ocr-suggestions', route => {
    requests.push(route.request().postDataJSON());
    const {status, body} = answer();
    return route.fulfill({status, json: body});
  });
  return requests;
}

async function drawText(page: Page) {
  await page.getByRole('button', {name: 'Text region'}).click();
  await drawBox(page, 330, 270, 380, 295);
}

test('a new text box is filled with the recognized text, which stays editable and saves', async ({page, request}) => {
  const {id} = await preparedDrawing(request);
  const requests = await withOcr(page, () => ({status: 200, body: {text: '62.565', confidence: 88}}));
  await openEditor(page, id);
  await drawText(page);

  const input = page.locator('input.transcription');
  await expect(input).toHaveValue('62.565');
  await expect(page.getByRole('button', {name: /^(Use|Dismiss)$/})).toHaveCount(0);
  expect(requests).toHaveLength(1);
  expect(requests[0]).toMatchObject({page_id: expect.any(String), polygon: expect.any(Array)});
  await saved(page);
  const region = (await serverDocument(request, id)).regions[0];
  expect(region.transcription).toBe('62.565');
  expect(region.origin).toBe('MANUAL');

  await input.fill('62.566');  // a wrong read is simply corrected
  await saved(page);
  expect((await serverDocument(request, id)).regions[0].transcription).toBe('62.566');
});

test('text typed before the read finishes is never overwritten', async ({page, request}) => {
  const {id} = await preparedDrawing(request);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(/\/api\/annotation\/drawings\/[0-9a-f-]{36}$/, async route => {
    const response = await route.fetch();
    await route.fulfill({response, json: {...(await response.json()), ocr_enabled: true}});
  });
  await page.route('**/ocr-suggestions', async route => {
    await gate;
    await route.fulfill({json: {text: '99.9', confidence: 90}});
  });
  await openEditor(page, id);
  await drawText(page);
  const input = page.locator('input.transcription');
  await expect(page.getByText('Reading text…')).toBeVisible();
  await input.fill('Ø20');
  release();
  await expect(page.getByText('Reading text…')).toHaveCount(0);
  await expect(input).toHaveValue('Ø20');
  await saved(page);
  expect((await serverDocument(request, id)).regions[0].transcription).toBe('Ø20');
});

test('a failed read leaves the field empty and typing still works', async ({page, request}) => {
  const {id} = await preparedDrawing(request);
  await withOcr(page, () => ({status: 503, body: {error: {code: 'OCR_UNAVAILABLE', message: 'no engine'}}}));
  await openEditor(page, id);
  await drawText(page);
  const input = page.locator('input.transcription');
  await expect(input).toBeFocused();
  await expect(page.getByText('Reading text…')).toHaveCount(0);
  await expect(input).toHaveValue('');
  await input.fill('abc');
  await saved(page);
  expect((await serverDocument(request, id)).regions[0].transcription).toBe('abc');
});
