import { execFileSync } from 'node:child_process';
import { expect, type APIRequestContext, type Page } from '@playwright/test';

const API = 'http://127.0.0.1:8011/api/annotation';

/** A small generated PDF (never a customer drawing). Unique content so every test gets a new drawing. */
export function makePdf(pages = 1): Buffer {
  const script = `
import io, sys, uuid
from reportlab.pdfgen import canvas
b = io.BytesIO(); c = canvas.Canvas(b, pagesize=(842, 595))
for i in range(${pages}):
    c.rect(30, 30, 782, 535); c.setFont('Helvetica', 14); c.drawString(340, 310, 'O20 +0.25')
    c.drawString(60, 90, 'NOTE ' + uuid.uuid4().hex[:8]); c.showPage()
c.save(); sys.stdout.buffer.write(b.getvalue())`;
  return execFileSync('.venv/bin/python', ['-c', script]);
}

export async function preparedDrawing(request: APIRequestContext, pages = 1): Promise<{id: string; pageIds: string[]}> {
  const upload = await request.post(`${API}/drawings`, {multipart: {file: {name: 'e2e.pdf', mimeType: 'application/pdf', buffer: makePdf(pages)}}});
  expect(upload.ok()).toBeTruthy();
  const id = (await upload.json()).drawing.id;
  await expect.poll(async () => (await (await request.get(`${API}/drawings/${id}`)).json()).drawing.preparation_state,
    {timeout: 30_000}).toBe('READY');
  const detail = await (await request.get(`${API}/drawings/${id}`)).json();
  return {id, pageIds: detail.pages.map((p: {id: string}) => p.id)};
}

export async function serverDocument(request: APIRequestContext, id: string) {
  return (await request.get(`${API}/drawings/${id}/document`)).json();
}

export async function openEditor(page: Page, id: string) {
  await page.goto(`/#/drawings/${id}`);
  await expect(page.locator('.canvas-page img')).toBeVisible();
  await expect(page.getByRole('button', {name: 'Text region'})).toBeEnabled({timeout: 20_000});
}

/** Drag a box in canonical page coordinates. */
export async function drawBox(page: Page, x1: number, y1: number, x2: number, y2: number, pageWidth = 842, pageHeight = 595) {
  const box = (await page.locator('.canvas-page img').boundingBox())!;
  const at = (x: number, y: number) => [box.x + x / pageWidth * box.width, box.y + y / pageHeight * box.height] as const;
  await page.mouse.move(...at(x1, y1));
  await page.mouse.down();
  await page.mouse.move(...at((x1 + x2) / 2, (y1 + y2) / 2), {steps: 3});
  await page.mouse.move(...at(x2, y2), {steps: 3});
  await page.mouse.up();
}

export async function clickAt(page: Page, x: number, y: number, modifiers: ('Shift')[] = [], pageWidth = 842, pageHeight = 595) {
  const box = (await page.locator('.canvas-page img').boundingBox())!;
  for (const key of modifiers) await page.keyboard.down(key);
  await page.mouse.click(box.x + x / pageWidth * box.width, box.y + y / pageHeight * box.height);
  for (const key of modifiers) await page.keyboard.up(key);
}

export async function addTextRegion(page: Page, rect: [number, number, number, number], text: string) {
  await page.getByRole('button', {name: 'Text region'}).click();
  await drawBox(page, ...rect);
  const input = page.locator('input.transcription');
  await expect(input).toBeFocused();
  await input.fill(text);
}

export const saved = (page: Page) => expect(page.locator('.save-indicator').first()).toHaveText(/^Saved/, {timeout: 15_000});
